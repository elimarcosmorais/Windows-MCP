"""Snapshot's window pre-pass must be bounded in wall time, like the tree capture is.

`Tree.get_state` runs on a thread the caller can stop waiting for, but the pre-pass that
feeds it — get_controls_handles, get_windows, get_active_window — ran on the calling
thread, before any deadline existed. Measured against a VS Code window wedged in
TextPattern.GetVisibleRanges, ControlFromHandle inside get_windows blocked for 15.4s
there, with the walk deadline and the capture ceiling both intact: they cover
`Tree.get_state`, which had not been called yet.

So the pre-pass gets the same treatment, and these tests pin what the tool reports when
it has to be given up on: an unknown window list, not an empty desktop, and no walk into
the provider that just swallowed the pre-pass.
"""

import threading
import time

import pytest

from windows_mcp.desktop import service as desktop_service
from windows_mcp.desktop.service import Desktop
from windows_mcp.desktop.views import Status, Window
from windows_mcp.tree import worker as tree_worker
from windows_mcp.tree.budget import TRUNCATION_REASON_ABANDONED
from windows_mcp.tree.views import BoundingBox


@pytest.fixture(autouse=True)
def _forget_abandoned_threads():
    """The abandoned-thread registry is module state — no test may inherit it."""
    tree_worker._abandoned_threads.clear()
    yield
    tree_worker._abandoned_threads.clear()


@pytest.fixture
def desktop(monkeypatch):
    """A Desktop whose environment-dependent reads are stubbed, leaving the pre-pass."""
    monkeypatch.delenv("WINDOWS_MCP_EXCLUDE_PROCESSES", raising=False)
    monkeypatch.delenv("WINDOWS_MCP_TREE_TIMEOUT", raising=False)
    instance = Desktop()
    monkeypatch.setattr(Desktop, "get_displays", lambda self: [])
    monkeypatch.setattr(Desktop, "get_cursor_location", lambda self: (0, 0))
    monkeypatch.setattr(desktop_service, "get_current_desktop", lambda: {"name": "Desktop 1"})
    monkeypatch.setattr(desktop_service, "get_all_desktops", lambda: [{"name": "Desktop 1"}])
    monkeypatch.setattr(
        Desktop, "get_screen_box", lambda self: BoundingBox(0, 0, 1920, 1080, 1920, 1080)
    )

    def unreachable_walk(*_args, **_kwargs):
        raise AssertionError("the tree walk was entered after the pre-pass was abandoned")

    monkeypatch.setattr(instance.tree, "get_state", unreachable_walk)
    return instance


def _window(handle: int = 100) -> Window:
    return Window(
        name="Visual Studio Code",
        is_browser=False,
        depth=0,
        status=Status.NORMAL,
        bounding_box=BoundingBox(0, 0, 800, 600, 800, 600),
        handle=handle,
        process_id=4321,
    )


class TestProbeWindowContext:
    def test_returns_the_three_stages_as_plain_data(self, monkeypatch):
        # Nothing built in the worker's COM apartment may escape into the caller's, so
        # the probe returns dataclasses and ints — never a UIA Control.
        instance = Desktop()
        window = _window()
        monkeypatch.setattr(Desktop, "get_controls_handles", lambda self: {100, 200})
        monkeypatch.setattr(
            Desktop, "get_windows", lambda self, controls_handles=None: ([window], {100})
        )
        monkeypatch.setattr(Desktop, "get_active_window", lambda self, windows=None: window)

        handles, windows, window_handles, active = instance._probe_window_context()

        assert handles == {100, 200}
        assert windows == [window]
        assert window_handles == {100}
        assert active is window

    def test_passes_the_enumerated_handles_down_the_stages(self, monkeypatch):
        # get_windows and get_active_window each re-enumerate when not given the previous
        # stage's result, which would double the cross-process calls.
        instance = Desktop()
        seen: dict[str, object] = {}
        window = _window()
        monkeypatch.setattr(Desktop, "get_controls_handles", lambda self: {100})

        def get_windows(self, controls_handles=None):
            seen["controls_handles"] = controls_handles
            return [window], {100}

        def get_active_window(self, windows=None):
            seen["windows"] = windows
            return window

        monkeypatch.setattr(Desktop, "get_windows", get_windows)
        monkeypatch.setattr(Desktop, "get_active_window", get_active_window)

        instance._probe_window_context()

        assert seen["controls_handles"] == {100}
        assert seen["windows"] == [window]


class TestPrePassCeiling:
    def test_runs_the_pre_pass_through_the_worker_with_the_tree_timeout(self, desktop, monkeypatch):
        monkeypatch.setenv("WINDOWS_MCP_TREE_TIMEOUT", "4")
        calls: dict[str, object] = {}

        def fake_run_capture(capture, timeout_sec, *, thread_name="", label=""):
            calls["timeout_sec"] = timeout_sec
            calls["thread_name"] = thread_name
            calls["label"] = label
            return (set(), [], set(), None), False

        monkeypatch.setattr(desktop_service, "run_capture", fake_run_capture)
        monkeypatch.setattr(desktop.tree, "get_state", lambda *a, **k: _empty_tree_state())

        desktop.get_state(use_vision=False, use_ui_tree=True)

        # The pre-pass has no soft deadline of its own to overshoot, so it takes the
        # walk limit as a flat ceiling, with none of the capture's grace added.
        assert calls["timeout_sec"] == 4.0
        assert calls["thread_name"] == "windows-mcp-window-probe"
        assert calls["label"] == "Desktop"

    def test_screenshot_fast_path_still_skips_the_pre_pass(self, desktop, monkeypatch):
        called: list[str] = []
        monkeypatch.setattr(
            desktop_service,
            "run_capture",
            lambda *a, **k: called.append("probe") or ((set(), [], set(), None), False),
        )

        state = desktop.get_state(use_vision=False, use_ui_tree=False)

        assert called == []
        assert state.windows_abandoned is False
        assert state.windows == []

    def test_abandoned_pre_pass_reports_an_unknown_window_list(self, desktop, monkeypatch):
        monkeypatch.setattr(desktop_service, "run_capture", lambda *a, **k: (None, True))

        state = desktop.get_state(use_vision=False, use_ui_tree=True)

        assert state.windows_abandoned is True
        assert state.windows == []
        assert state.active_window is None
        # The walk is not attempted: desktop.tree.get_state raises on contact, so
        # reaching this assertion is the proof.
        assert state.tree_state.truncated is True
        assert state.tree_state.truncation_reason == TRUNCATION_REASON_ABANDONED
        assert state.tree_state.status is False

    def test_abandoned_pre_pass_does_not_claim_an_empty_desktop(self, desktop, monkeypatch):
        monkeypatch.setattr(desktop_service, "run_capture", lambda *a, **k: (None, True))

        state = desktop.get_state(use_vision=False, use_ui_tree=True)

        windows_text = state.windows_to_string()
        assert "No windows found" not in windows_text
        assert "WINDOWS_MCP_EXCLUDE_PROCESSES" in windows_text
        assert "No active window found" not in state.active_window_to_string()

    def test_abandoned_note_names_the_limit_that_cut_the_capture(self, desktop, monkeypatch):
        monkeypatch.setenv("WINDOWS_MCP_TREE_TIMEOUT", "3")
        monkeypatch.setattr(desktop_service, "run_capture", lambda *a, **k: (None, True))

        state = desktop.get_state(use_vision=False, use_ui_tree=True)

        assert state.tree_state.timeout_sec == 3.0
        assert "3s" in state.tree_state.interactive_elements_to_string()

    def test_a_healthy_pre_pass_reaches_the_walk_unchanged(self, desktop, monkeypatch):
        window = _window()
        monkeypatch.setattr(
            desktop_service,
            "run_capture",
            lambda *a, **k: (({100, 200}, [window], {100}, window), False),
        )
        seen: dict[str, object] = {}

        def walk(active_handle, other_handles, use_dom=False):
            seen["active_handle"] = active_handle
            seen["other_handles"] = sorted(other_handles)
            return _empty_tree_state()

        monkeypatch.setattr(desktop.tree, "get_state", walk)

        state = desktop.get_state(use_vision=False, use_ui_tree=True)

        assert state.windows_abandoned is False
        assert state.active_window is window
        assert seen["active_handle"] == 100
        # The active window's own handle is not walked twice.
        assert seen["other_handles"] == [200]

    def test_the_ceiling_bounds_a_pre_pass_that_never_returns(self, desktop, monkeypatch):
        # The end-to-end claim, with a real thread: a pre-pass stuck the way a wedged
        # provider sticks must not hold the caller past the ceiling.
        monkeypatch.setenv("WINDOWS_MCP_TREE_TIMEOUT", "1")
        release = threading.Event()

        def wedged_probe(self):
            release.wait(60)
            return set(), [], set(), None

        monkeypatch.setattr(Desktop, "_probe_window_context", wedged_probe)
        started = time.monotonic()
        try:
            state = desktop.get_state(use_vision=False, use_ui_tree=True)
            elapsed = time.monotonic() - started

            assert state.windows_abandoned is True
            assert 1.0 <= elapsed < 5.0, f"pre-pass held the caller for {elapsed:.2f}s"
            assert tree_worker.abandoned_capture_count() == 1
        finally:
            release.set()

    def test_an_abandoned_pre_pass_still_returns_the_screenshot_context(self, desktop, monkeypatch):
        # Losing the window list must not cost the caller the rest of the snapshot: the
        # virtual-desktop context and cursor are read outside the pre-pass.
        monkeypatch.setattr(desktop_service, "run_capture", lambda *a, **k: (None, True))

        state = desktop.get_state(use_vision=False, use_ui_tree=True)

        assert state.active_desktop == {"name": "Desktop 1"}
        assert state.cursor_position == (0, 0)


def _empty_tree_state():
    from windows_mcp.tree.views import TreeState

    return TreeState(status=True)
