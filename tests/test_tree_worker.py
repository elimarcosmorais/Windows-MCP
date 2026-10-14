import threading
import time

import pytest

from windows_mcp.tree.budget import TRUNCATION_REASON_ABANDONED
from windows_mcp.tree.service import Tree
from windows_mcp.tree.views import TreeState, _truncation_note
from windows_mcp.tree import worker as tree_worker
from windows_mcp.tree.worker import (
    CAPTURE_GRACE_SEC,
    MAX_ABANDONED_CAPTURES,
    abandoned_capture_count,
    resolve_capture_timeout,
    run_capture,
)


@pytest.fixture(autouse=True)
def _forget_abandoned_threads():
    """The abandoned-thread registry is module state — no test may inherit it."""
    tree_worker._abandoned_threads.clear()
    yield
    tree_worker._abandoned_threads.clear()


class StuckThread:
    """Stands in for a capture thread still blocked in COM."""

    def __init__(self, alive: bool = True) -> None:
        self.name = "windows-mcp-tree-capture"
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


class TestResolveCaptureTimeout:
    def test_adds_grace_to_the_walk_deadline(self):
        assert resolve_capture_timeout(10.0) == 10.0 + CAPTURE_GRACE_SEC

    def test_grace_separates_a_slow_call_from_a_wedged_one(self):
        # A healthy provider peaked at ~3.7s in one call; a wedged one blocked 12-20s.
        # The grace has to cover the former without covering the latter.
        assert CAPTURE_GRACE_SEC > 3.7
        assert CAPTURE_GRACE_SEC < 12.0

    def test_non_positive_walk_deadline_still_yields_the_grace(self):
        assert resolve_capture_timeout(0.0) == CAPTURE_GRACE_SEC
        assert resolve_capture_timeout(-5.0) == CAPTURE_GRACE_SEC


class TestRunCapture:
    def test_returns_the_capture_value(self):
        value, abandoned = run_capture(lambda: "tree", timeout_sec=5.0)
        assert value == "tree"
        assert abandoned is False

    def test_runs_on_another_thread(self):
        caller = threading.get_ident()
        value, abandoned = run_capture(threading.get_ident, timeout_sec=5.0)
        assert abandoned is False
        assert value != caller

    def test_thread_is_named_and_is_a_daemon(self):
        def capture() -> tuple[str, bool]:
            current = threading.current_thread()
            return current.name, current.daemon

        (name, daemon), abandoned = run_capture(capture, timeout_sec=5.0, thread_name="probe")
        assert abandoned is False
        assert name == "probe"
        # A wedged capture thread can never be joined, so it must not hold the
        # interpreter open at shutdown.
        assert daemon is True

    def test_abandons_a_capture_that_does_not_return(self):
        release = threading.Event()
        started = threading.Event()

        def capture() -> str:
            started.set()
            release.wait(30)
            return "too late"

        try:
            began = time.monotonic()
            value, abandoned = run_capture(capture, timeout_sec=0.2)
            waited = time.monotonic() - began
            assert started.wait(5) is True
            assert abandoned is True
            assert value is None
            # The point of the whole module: the caller stops waiting.
            assert waited < 5.0
        finally:
            release.set()

    def test_reraises_what_the_capture_raised(self):
        def capture() -> None:
            raise RuntimeError("provider said no")

        with pytest.raises(RuntimeError, match="provider said no"):
            run_capture(capture, timeout_sec=5.0)

    def test_registers_the_abandoned_thread(self):
        release = threading.Event()
        try:
            run_capture(lambda: release.wait(30), timeout_sec=0.2)
            assert abandoned_capture_count() == 1
        finally:
            release.set()

    def test_a_successful_capture_registers_nothing(self):
        run_capture(lambda: "tree", timeout_sec=5.0)
        assert abandoned_capture_count() == 0


class TestAbandonedThreadCap:
    """Abandoning leaks a thread every time, so the leak has to have a ceiling."""

    def test_counts_only_the_threads_still_blocked(self):
        tree_worker._abandoned_threads.extend(
            [StuckThread(), StuckThread(alive=False), StuckThread()]
        )
        assert abandoned_capture_count() == 2
        # The finished one is forgotten, not merely skipped.
        assert len(tree_worker._abandoned_threads) == 2

    def test_at_the_cap_the_capture_is_not_even_attempted(self):
        tree_worker._abandoned_threads.extend(StuckThread() for _ in range(MAX_ABANDONED_CAPTURES))
        ran = threading.Event()

        began = time.monotonic()
        value, abandoned = run_capture(ran.set, timeout_sec=30.0)
        waited = time.monotonic() - began

        assert abandoned is True
        assert value is None
        assert ran.is_set() is False
        # The whole point: no new thread, and no waiting for one either.
        assert waited < 1.0
        assert abandoned_capture_count() == MAX_ABANDONED_CAPTURES

    def test_below_the_cap_captures_still_run(self):
        tree_worker._abandoned_threads.extend(
            StuckThread() for _ in range(MAX_ABANDONED_CAPTURES - 1)
        )
        value, abandoned = run_capture(lambda: "tree", timeout_sec=5.0)
        assert (value, abandoned) == ("tree", False)

    def test_captures_resume_once_the_stuck_threads_unblock(self):
        tree_worker._abandoned_threads.extend(
            StuckThread(alive=False) for _ in range(MAX_ABANDONED_CAPTURES)
        )
        value, abandoned = run_capture(lambda: "tree", timeout_sec=5.0)
        assert (value, abandoned) == ("tree", False)


class TestTreeGetStateOffload:
    """`Tree.get_state` offloads to the worker and degrades honestly when it has to."""

    def _tree(self) -> Tree:
        # __init__ reaches for a live Desktop and COM; the wrapper under test touches
        # neither, so build the instance without running it.
        return Tree.__new__(Tree)

    def test_returns_what_the_capture_produced(self, monkeypatch):
        captured = TreeState(status=True, element_limit=500)
        seen: dict[str, object] = {}

        def fake_capture(self, active, others, use_dom=False):
            seen["args"] = (active, others, use_dom)
            return captured

        monkeypatch.setattr(Tree, "_capture_state", fake_capture)
        state = self._tree().get_state(123, [456], use_dom=True)

        assert state is captured
        assert seen["args"] == (123, [456], True)

    def test_abandoned_capture_yields_an_empty_truncated_state(self, monkeypatch):
        monkeypatch.setenv("WINDOWS_MCP_TREE_TIMEOUT", "0.05")
        monkeypatch.setenv("WINDOWS_MCP_MAX_TREE_ELEMENTS", "400")
        monkeypatch.setattr("windows_mcp.tree.service.resolve_capture_timeout", lambda _: 0.1)
        release = threading.Event()

        def wedged_capture(self, active, others, use_dom=False):
            release.wait(30)
            return TreeState(status=True)

        monkeypatch.setattr(Tree, "_capture_state", wedged_capture)
        try:
            state = self._tree().get_state(123, [])
        finally:
            release.set()

        assert state.status is False
        assert state.truncated is True
        assert state.truncation_reason == TRUNCATION_REASON_ABANDONED
        assert state.element_limit == 400
        assert state.interactive_nodes == []
        assert state.scrollable_nodes == []
        assert state.semantic_tree_root is None

    def test_abandoned_capture_propagates_nothing_from_the_zombie(self, monkeypatch):
        # The abandoned thread finishing later must not change what the caller got.
        monkeypatch.setattr("windows_mcp.tree.service.resolve_capture_timeout", lambda _: 0.1)
        done = threading.Event()

        def late_capture(self, active, others, use_dom=False):
            time.sleep(0.4)
            done.set()
            return TreeState(status=True, element_limit=999)

        monkeypatch.setattr(Tree, "_capture_state", late_capture)
        state = self._tree().get_state(123, [])
        assert done.wait(5) is True
        assert state.truncated is True
        assert state.element_limit != 999

    def test_capture_error_still_reaches_the_caller(self, monkeypatch):
        def failing_capture(self, active, others, use_dom=False):
            raise RuntimeError("UIA is gone")

        monkeypatch.setattr(Tree, "_capture_state", failing_capture)
        with pytest.raises(RuntimeError, match="UIA is gone"):
            self._tree().get_state(123, [])


class TestAbandonedTruncationNote:
    def test_note_names_the_cause_and_the_escape_hatch(self):
        note = _truncation_note(500, TRUNCATION_REASON_ABANDONED, 15.0)
        assert "no elements captured" in note
        assert "stopped answering UI Automation" in note
        assert "abandoned after 15s" in note
        assert "WINDOWS_MCP_EXCLUDE_PROCESSES" in note
        # The element budget never fired here, so naming it would be a lie.
        assert "500-element" not in note

    def test_an_abandoned_capture_says_so_instead_of_looking_empty(self):
        # The whole output of an abandoned capture is empty lists. Rendering those as
        # "No interactive elements" alone would report an empty window, which is a
        # different and false claim.
        state = TreeState(
            status=False,
            truncated=True,
            truncation_reason=TRUNCATION_REASON_ABANDONED,
            timeout_sec=15.0,
            element_limit=500,
        )
        for rendered in (
            state.interactive_elements_to_string(),
            state.scrollable_elements_to_string(),
            state.semantic_tree_to_string(),
        ):
            assert rendered.startswith("No ")
            assert "stopped answering UI Automation" in rendered

    def test_empty_renders_are_untouched_for_the_other_reasons(self):
        # The element and time limits keep upstream's rendering: a capture they stopped
        # has already collected whatever it found.
        for reason in ("", "elements", "time", "elements+time"):
            state = TreeState(truncated=True, truncation_reason=reason, element_limit=10)
            assert state.interactive_elements_to_string() == "No interactive elements"
            assert state.scrollable_elements_to_string() == "No scrollable elements"
            assert state.semantic_tree_to_string() == "No elements"

    def test_other_reasons_keep_their_notes(self):
        # Guards the one risk of adding a branch: the existing wordings must not move.
        assert "500-element capture limit" in _truncation_note(500, "", 0.0)
        assert "10s capture time limit" in _truncation_note(500, "time", 10.0)
