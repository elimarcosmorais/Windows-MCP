"""The Snapshot window pre-pass must honour WINDOWS_MCP_EXCLUDE_PROCESSES.

The exclusion list used to be read only inside the tree walk, while the pre-pass that
runs before any walk called ControlFromHandle on every visible window. Measured against
a VS Code window whose provider had stopped answering, that pre-pass blocked for 15.4s
*with the exclusion set* — the one mitigation the documentation recommends did not cover
the first UI Automation call the tool makes.

These tests pin both halves of the fix: an excluded window is described without a single
UI Automation call, and it is still described, because dropping it from the listing would
cost the caller the ability to see or switch to that window.
"""

import pytest

from windows_mcp.desktop import service as desktop_service
from windows_mcp.desktop.service import Desktop, _NOT_EXCLUDED
from windows_mcp.desktop.views import Status

WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
APP_WINDOW_STYLE = WS_MINIMIZEBOX | WS_MAXIMIZEBOX


class Uia:
    """Stands in for the uia module, counting any call that would enter a provider."""

    def __init__(self, iconic: set[int] | None = None, foreground: int = 0) -> None:
        self.control_from_handle_calls: list[int] = []
        self.iconic = iconic or set()
        self.foreground = foreground

    def ControlFromHandle(self, handle: int):
        self.control_from_handle_calls.append(handle)
        raise AssertionError(f"ControlFromHandle entered an excluded provider: {handle}")

    def GetForegroundWindow(self) -> int:
        return self.foreground

    def IsIconic(self, handle: int) -> bool:
        return handle in self.iconic

    def IsZoomed(self, handle: int) -> bool:
        return False

    def IsWindowVisible(self, handle: int) -> bool:
        return True


@pytest.fixture
def win32(monkeypatch):
    """Patch the Win32 surface the fallback path reads, keyed by handle."""
    titles: dict[int, str] = {}
    styles: dict[int, int] = {}
    rects: dict[int, tuple[int, int, int, int]] = {}
    pids: dict[int, int] = {}
    ancestors: dict[int, int] = {}

    class Win32Gui:
        @staticmethod
        def GetWindowText(handle: int) -> str:
            return titles.get(handle, "")

        @staticmethod
        def GetWindowLong(handle: int, _index: int) -> int:
            return styles.get(handle, 0)

        @staticmethod
        def GetWindowRect(handle: int) -> tuple[int, int, int, int]:
            return rects.get(handle, (0, 0, 0, 0))

        @staticmethod
        def GetAncestor(handle: int, _flag: int) -> int:
            return ancestors.get(handle, handle)

    class Win32Process:
        @staticmethod
        def GetWindowThreadProcessId(handle: int) -> tuple[int, int]:
            return 0, pids.get(handle, 0)

    monkeypatch.setattr(desktop_service, "win32gui", Win32Gui)
    monkeypatch.setattr(desktop_service, "win32process", Win32Process)
    monkeypatch.setattr(desktop_service.Browser, "has_process", staticmethod(lambda name: False))

    def register(
        handle: int,
        title: str = "Editor",
        style: int = APP_WINDOW_STYLE,
        rect: tuple[int, int, int, int] = (0, 0, 800, 600),
        pid: int = 4321,
        root: int | None = None,
    ) -> int:
        titles[handle] = title
        styles[handle] = style
        rects[handle] = rect
        pids[handle] = pid
        if root is not None:
            ancestors[handle] = root
        return handle

    return register


@pytest.fixture
def desktop(monkeypatch):
    monkeypatch.delenv("WINDOWS_MCP_EXCLUDE_PROCESSES", raising=False)
    return Desktop()


def _exclude(monkeypatch, names: str, owners: dict[int, str]):
    monkeypatch.setenv("WINDOWS_MCP_EXCLUDE_PROCESSES", names)
    monkeypatch.setattr(desktop_service, "_window_process_name", lambda handle: owners.get(handle))


class TestWin32WindowDescription:
    def test_describes_an_excluded_window_without_touching_uia(self, desktop, win32, monkeypatch):
        uia = Uia()
        monkeypatch.setattr(desktop_service, "uia", uia)
        win32(100, title="CHANGELOG.md - Visual Studio Code", rect=(10, 20, 810, 620), pid=7516)

        window = desktop._win32_window(100, depth=3, process_name="Code.exe")

        assert window is not None
        assert window.name == "CHANGELOG.md - Visual Studio Code"
        assert window.handle == 100
        assert window.process_id == 7516
        assert window.depth == 3
        assert window.status == Status.NORMAL
        assert (window.bounding_box.left, window.bounding_box.top) == (10, 20)
        assert (window.bounding_box.width, window.bounding_box.height) == (800, 600)
        assert uia.control_from_handle_calls == []

    def test_minimized_window_reports_an_empty_box_like_uia_does(self, desktop, win32, monkeypatch):
        # Win32 hands back the off-screen (-32000, -32000) placeholder for a minimized
        # window where UI Automation reports an empty rect. Measured on this platform;
        # the UIA value is what get_windows stores, so the fallback matches it.
        monkeypatch.setattr(desktop_service, "uia", Uia(iconic={100}))
        win32(100, rect=(-32000, -32000, -31801, -31966))

        window = desktop._win32_window(100, depth=0, process_name="Code.exe")

        assert window is not None
        assert window.status == Status.MINIMIZED
        assert window.bounding_box.left == 0
        assert window.bounding_box.right == 0
        assert window.bounding_box.width == 0

    @pytest.mark.parametrize(
        "title,style,rect,why",
        [
            ("", APP_WINDOW_STYLE, (0, 0, 800, 600), "a blank title marks an overlay"),
            ("   ", APP_WINDOW_STYLE, (0, 0, 800, 600), "whitespace is still blank"),
            ("NVIDIA Overlay", APP_WINDOW_STYLE, (0, 0, 800, 600), "named overlay"),
            ("Tooltip", 0, (0, 0, 800, 600), "no minimize/maximize box is not an app window"),
            ("Panel", WS_MINIMIZEBOX, (0, 0, 800, 600), "needs both style bits"),
            ("Ghost", APP_WINDOW_STYLE, (5, 5, 5, 5), "empty rect while not minimized"),
        ],
    )
    def test_applies_the_same_filters_get_windows_applies(
        self, desktop, win32, monkeypatch, title, style, rect, why
    ):
        monkeypatch.setattr(desktop_service, "uia", Uia())
        win32(100, title=title, style=style, rect=rect)

        assert desktop._win32_window(100, depth=0, process_name="Code.exe") is None, why

    def test_survives_a_window_that_disappears_mid_read(self, desktop, monkeypatch):
        class Vanished:
            @staticmethod
            def GetWindowText(_handle):
                raise OSError("invalid window handle")

            @staticmethod
            def GetWindowLong(_handle, _index):
                raise OSError("invalid window handle")

        monkeypatch.setattr(desktop_service, "uia", Uia())
        monkeypatch.setattr(desktop_service, "win32gui", Vanished)

        assert desktop._win32_window(100, depth=0, process_name="Code.exe") is None


class TestGetWindowsExclusion:
    def test_excluded_window_is_listed_without_entering_its_provider(
        self, desktop, win32, monkeypatch
    ):
        uia = Uia()
        monkeypatch.setattr(desktop_service, "uia", uia)
        win32(100, title="Visual Studio Code", pid=7516)
        _exclude(monkeypatch, "Code.exe", {100: "Code.exe"})

        windows, handles = desktop.get_windows(controls_handles={100})

        # ControlFromHandle raises on contact, so arriving here at all is the assertion:
        # the wedged call measured at 15.4s was never made.
        assert uia.control_from_handle_calls == []
        assert [window.name for window in windows] == ["Visual Studio Code"]
        assert handles == {100}

    def test_match_is_case_insensitive_like_the_walk(self, desktop, win32, monkeypatch):
        monkeypatch.setattr(desktop_service, "uia", Uia())
        win32(100, title="Visual Studio Code")
        _exclude(monkeypatch, "code.exe", {100: "Code.EXE"})

        windows, _ = desktop.get_windows(controls_handles={100})

        assert [window.name for window in windows] == ["Visual Studio Code"]

    def test_an_excluded_window_that_fails_a_filter_is_dropped_not_entered(
        self, desktop, win32, monkeypatch
    ):
        uia = Uia()
        monkeypatch.setattr(desktop_service, "uia", uia)
        win32(100, title="")  # overlay: no title
        _exclude(monkeypatch, "Code.exe", {100: "Code.exe"})

        windows, handles = desktop.get_windows(controls_handles={100})

        assert windows == []
        assert handles == set()
        assert uia.control_from_handle_calls == []

    def test_a_process_not_on_the_list_still_goes_through_uia(self, desktop, win32, monkeypatch):
        uia = Uia()
        monkeypatch.setattr(desktop_service, "uia", uia)
        win32(100, title="Notepad")
        _exclude(monkeypatch, "Code.exe", {100: "notepad.exe"})

        windows, _ = desktop.get_windows(controls_handles={100})

        # The UIA path is the default and must stay the default: ControlFromHandle was
        # reached (and raised, which get_windows swallows per window).
        assert uia.control_from_handle_calls == [100]
        assert windows == []

    def test_an_unresolvable_owner_is_treated_as_not_excluded(self, desktop, win32, monkeypatch):
        uia = Uia()
        monkeypatch.setattr(desktop_service, "uia", uia)
        win32(100, title="Notepad")
        _exclude(monkeypatch, "Code.exe", {})  # process name resolves to None

        desktop.get_windows(controls_handles={100})

        assert uia.control_from_handle_calls == [100]

    def test_empty_exclusion_list_costs_no_process_lookup(self, desktop, win32, monkeypatch):
        uia = Uia()
        monkeypatch.setattr(desktop_service, "uia", uia)
        lookups: list[int] = []
        monkeypatch.setenv("WINDOWS_MCP_EXCLUDE_PROCESSES", "  ,  ")
        monkeypatch.setattr(
            desktop_service, "_window_process_name", lambda handle: lookups.append(handle)
        )

        desktop.get_windows(controls_handles={100})

        assert lookups == []
        assert uia.control_from_handle_calls == [100]


class TestExcludedForegroundWindow:
    def test_returns_not_excluded_when_the_list_is_empty(self, desktop, monkeypatch):
        monkeypatch.delenv("WINDOWS_MCP_EXCLUDE_PROCESSES", raising=False)

        assert desktop._excluded_foreground_window([]) is _NOT_EXCLUDED

    def test_returns_not_excluded_on_a_null_foreground_handle(self, desktop, monkeypatch):
        monkeypatch.setattr(desktop_service, "uia", Uia(foreground=0))
        _exclude(monkeypatch, "Code.exe", {})

        assert desktop._excluded_foreground_window([]) is _NOT_EXCLUDED

    def test_returns_not_excluded_for_an_ordinary_process(self, desktop, monkeypatch):
        monkeypatch.setattr(desktop_service, "uia", Uia(foreground=100))
        _exclude(monkeypatch, "Code.exe", {100: "notepad.exe"})

        assert desktop._excluded_foreground_window([]) is _NOT_EXCLUDED

    def test_reuses_the_already_enumerated_window(self, desktop, win32, monkeypatch, sample_window):
        monkeypatch.setattr(desktop_service, "uia", Uia(foreground=sample_window.handle))
        win32(sample_window.handle)
        _exclude(monkeypatch, "Code.exe", {sample_window.handle: "Code.exe"})

        assert desktop._excluded_foreground_window([sample_window]) is sample_window

    def test_resolves_a_child_handle_to_its_root_with_win32(self, desktop, win32, monkeypatch):
        # get_foreground_window walks to the top-level window through UIA parents; for an
        # excluded process GetAncestor(GA_ROOT) answers the same question with Win32.
        uia = Uia(foreground=555)
        monkeypatch.setattr(desktop_service, "uia", uia)
        win32(555, title="child", root=100)
        win32(100, title="Visual Studio Code")
        _exclude(monkeypatch, "Code.exe", {555: "Code.exe"})

        window = desktop._excluded_foreground_window([])

        assert window is not None
        assert window.handle == 100
        assert window.name == "Visual Studio Code"
        assert uia.control_from_handle_calls == []

    def test_get_active_window_takes_the_win32_path_for_an_excluded_process(
        self, desktop, win32, monkeypatch
    ):
        uia = Uia(foreground=100)
        monkeypatch.setattr(desktop_service, "uia", uia)
        win32(100, title="Visual Studio Code", pid=7516)
        _exclude(monkeypatch, "Code.exe", {100: "Code.exe"})

        window = desktop.get_active_window(windows=[])

        assert window is not None
        assert window.handle == 100
        assert window.process_id == 7516
        assert uia.control_from_handle_calls == []
