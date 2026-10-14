from windows_mcp.tree.views import TreeState, BoundingBox
from dataclasses import dataclass
from tabulate import tabulate
from PIL.Image import Image
from enum import Enum


class Browser(Enum):
    CHROME = "chrome"
    EDGE = "msedge"
    FIREFOX = "firefox"

    @classmethod
    def has_process(cls, process_name: str) -> bool:
        if not hasattr(cls, "_process_names"):
            cls._process_names = {f"{b.value}.exe" for b in cls}
        return process_name.lower() in cls._process_names


class Status(Enum):
    MAXIMIZED = "Maximized"
    MINIMIZED = "Minimized"
    NORMAL = "Normal"
    HIDDEN = "Hidden"


@dataclass
class Window:
    name: str
    is_browser: bool
    depth: int
    status: Status
    bounding_box: BoundingBox
    handle: int
    process_id: int

    def to_row(self):
        return [
            self.name,
            self.depth,
            self.status.value,
            self.bounding_box.width,
            self.bounding_box.height,
            self.handle,
        ]


@dataclass
class Size:
    width: int
    height: int

    def to_string(self):
        return f"({self.width},{self.height})"


@dataclass
class Display:
    index: int
    device_name: str
    bounding_box: BoundingBox
    primary: bool


@dataclass
class DesktopState:
    active_desktop: dict
    all_desktops: list[dict]
    active_window: Window | None
    windows: list[Window]
    screenshot: Image | None = None
    cursor_position: tuple[int, int] | None = None
    screenshot_original_size: Size | None = None
    screenshot_scale: float | None = None
    screenshot_region: BoundingBox | None = None
    screenshot_displays: list[int] | None = None
    available_displays: list[Display] | None = None
    screenshot_backend: str | None = None
    tree_state: TreeState | None = None
    #: Whether the window pre-pass was abandoned mid-call because an application stopped
    #: answering UI Automation. The window list is then unknown, not empty, and saying
    #: "no windows found" would assert something false about the desktop.
    windows_abandoned: bool = False
    capture_sec: float = 0.0

    def active_desktop_to_string(self):
        desktop_name = self.active_desktop.get("name")
        headers = ["Name"]
        return tabulate([[desktop_name]], headers=headers, tablefmt="simple")

    def desktops_to_string(self):
        headers = ["Name"]
        rows = [[desktop.get("name")] for desktop in self.all_desktops]
        return tabulate(rows, headers=headers, tablefmt="simple")

    #: Said instead of "no windows found" when the pre-pass was abandoned. It names the
    #: cause and the one setting that keeps the scan out of the offending process, the
    #: same guidance an abandoned tree capture gives.
    _WINDOWS_ABANDONED_NOTE = (
        "Window list unavailable: an application stopped answering UI Automation and the "
        "window scan was abandoned. This is not an empty desktop. Retry, or keep the scan "
        "out of that process with WINDOWS_MCP_EXCLUDE_PROCESSES."
    )

    def active_window_to_string(self):
        if not self.active_window:
            if self.windows_abandoned:
                return "Active window unavailable: the window scan was abandoned — see Windows."
            return "No active window found"
        headers = ["Name", "Depth", "Status", "Width", "Height", "Handle"]
        return tabulate([self.active_window.to_row()], headers=headers, tablefmt="simple")

    def windows_to_string(self):
        if not self.windows:
            if self.windows_abandoned:
                return self._WINDOWS_ABANDONED_NOTE
            return "No windows found"
        headers = ["Name", "Depth", "Status", "Width", "Height", "Handle"]
        rows = [window.to_row() for window in self.windows]
        return tabulate(rows, headers=headers, tablefmt="simple")
