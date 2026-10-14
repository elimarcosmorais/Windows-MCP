"""Runs a tree capture on a worker thread, so one wedged COM call cannot pin the server.

TreeWalkDeadline bounds a walk *between* UI Automation calls, which is where a merely
slow provider can be stopped. It cannot bound a provider that stops answering *inside*
a single cross-process call, and that is the shape of the worst failure measured:
against a VS Code window with ``editor.accessibilitySupport`` enabled, both
``TextPattern.GetVisibleRanges`` and ``IUIAutomationElement::GetCurrentPattern`` sat
12-20s in one call. A capture asked for a 3.0s deadline returned at 20.4s, and the
target window itself reported ``IsHungAppWindow`` for 13 consecutive seconds. Neither
limit in ``budget`` can see that: the element count stays at zero the whole time,
because a wedged call appends nothing.

The only place a caller can stop waiting for a COM call is outside the thread making
it. So the capture runs on its own thread, with its own COM apartment, and the caller
waits with a hard timeout. A thread blocked in a COM call cannot be killed safely —
there is no interruption point — so it is abandoned: daemon, never reused, left to
unblock on its own or to die with the process.

Abandoning leaks a thread, every time — measured against a wedged provider, each
capture leaves exactly one thread blocked in COM and none of them ever came back. So
the leak is bounded: at `MAX_ABANDONED_CAPTURES` live abandoned threads, a capture is
not attempted at all.

Two further consequences a caller should know about:

- An abandoned capture still owns the Tree instance it was given, and it writes
  ``Tree.dom`` and the element budget whenever it finally unblocks. That write can land
  after a later capture has finished, so a ``Tree.dom`` read can see the abandoned
  capture's DOM instead of the current one. The window is narrow — every capture resets
  both as its first act, and callers read ``Tree.dom`` immediately after their own
  capture returns — and the alternative to abandoning the thread is waiting for it
  forever, which is the bug being fixed. Closing it properly means giving each capture
  its own DOM state, which is a change to the walk, not to this module.
- UI Automation client objects built in one apartment are used from another when the
  caller later reads ``Tree.dom``. Measured working on this platform (identical words
  and bounding boxes across apartments), but it is the reason the grace period below
  exists rather than a zero-tolerance hard cut.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Seconds the hard timeout allows on top of the walk deadline. The walk deadline is
#: only read between COM calls, so a capture legitimately overshoots it by whatever the
#: call in flight still needs. Measured on a healthy provider that peak is ~3.7s, while
#: a wedged one blocks for 12-20s, so this separates "slow, let it finish and return a
#: partial tree" from "stopped answering, abandon it".
CAPTURE_GRACE_SEC = 5.0

#: How many abandoned capture threads may be alive before a new capture stops being
#: attempted. Abandoning is a leak by construction — measured against a wedged provider,
#: every capture leaves exactly one thread blocked in COM forever — so a client polling
#: Snapshot against that app would grow threads without limit and pay the full hard
#: timeout each call for nothing. At the cap, captures fail fast instead. This recovers
#: on its own: a thread that finally unblocks is forgotten, so captures resume once the
#: target starts answering again.
MAX_ABANDONED_CAPTURES = 4

_abandoned_lock = threading.Lock()
_abandoned_threads: list[threading.Thread] = []


def abandoned_capture_count() -> int:
    """How many abandoned capture threads are still blocked.

    Returns:
        The count, after forgetting the threads that have since unblocked.
    """
    with _abandoned_lock:
        _abandoned_threads[:] = [thread for thread in _abandoned_threads if thread.is_alive()]
        return len(_abandoned_threads)


def resolve_capture_timeout(walk_timeout_sec: float) -> float:
    """Hard wall-clock ceiling for a capture, given its walk deadline.

    Args:
        walk_timeout_sec: The walk deadline the capture runs under, in seconds.

    Returns:
        The deadline plus `CAPTURE_GRACE_SEC`, so a capture that is merely slow gets to
        return its partial tree and only a wedged one is abandoned.
    """
    return max(0.0, float(walk_timeout_sec)) + CAPTURE_GRACE_SEC


def run_capture(
    capture: Callable[[], T],
    timeout_sec: float,
    *,
    thread_name: str = "windows-mcp-tree-capture",
) -> tuple[T | None, bool]:
    """Run `capture` on a worker thread with its own COM apartment.

    Args:
        capture: The capture to run. Must create and use its own COM objects — it runs
            in a different apartment from the caller.
        timeout_sec: How long to wait before abandoning the thread.
        thread_name: Name for the worker thread, to make a wedged one identifiable in a
            stack dump.

    Returns:
        ``(value, abandoned)``. On success, the capture's return value and False. On
        timeout, ``(None, True)`` — the thread keeps running, unwatched. Also
        ``(None, True)`` without running `capture` at all when `MAX_ABANDONED_CAPTURES`
        threads are already stuck.

    Raises:
        BaseException: Whatever `capture` raised, re-raised on the calling thread, so
            offloading does not swallow a failure the caller used to see.
    """
    stuck = abandoned_capture_count()
    if stuck >= MAX_ABANDONED_CAPTURES:
        logger.warning(
            "[Tree] %d abandoned capture thread(s) still blocked in COM — skipping this "
            "capture instead of leaking another. Captures resume once they unblock.",
            stuck,
        )
        return None, True

    box: dict[str, object] = {}

    def worker() -> None:
        # Imported here so a test (or a non-Windows import of this module) does not
        # need comtypes present just to exercise the timeout logic.
        import comtypes

        initialized = False
        try:
            comtypes.CoInitializeEx()
            initialized = True
        except Exception as exc:  # pragma: no cover - platform specific
            # Worth capturing: the capture may still work (COM already initialized for
            # this thread by something else), so this is not a reason to bail out.
            logger.debug("[Tree] CoInitializeEx failed on capture thread: %s", exc)
        try:
            box["value"] = capture()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            box["error"] = exc
        finally:
            if initialized:
                try:
                    comtypes.CoUninitialize()
                except Exception as exc:  # pragma: no cover - platform specific
                    logger.debug("[Tree] CoUninitialize failed on capture thread: %s", exc)

    thread = threading.Thread(target=worker, name=thread_name, daemon=True)
    thread.start()
    thread.join(timeout_sec)
    if thread.is_alive():
        with _abandoned_lock:
            _abandoned_threads.append(thread)
        return None, True
    error = box.get("error")
    if error is not None:
        raise error  # type: ignore[misc]
    return box.get("value"), False  # type: ignore[return-value]
