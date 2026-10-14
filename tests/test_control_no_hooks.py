"""`--no-control` must install no input hooks at all.

The existing lifespan test asserts the disabled path never reaches
``_interactive_desktop``. This one watches the call that actually matters — the
``SetWindowsHookExW`` in :mod:`windows_mcp.desktop.control_win32` — so the
guarantee survives a refactor that stops consulting the desktop first. The probe
is installed on the shared ``_user32`` object, which is the same object
``control.py`` holds, so no hook anywhere in the package can slip past it.
"""

from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from windows_mcp import __main__ as cli
from windows_mcp.desktop import control, control_overlay, control_win32, service
from windows_mcp.infrastructure.config import ControlConfig
from windows_mcp.tools import control_status
from windows_mcp.tools.control_notifications import ControlNotifier

WH_MOUSE_LL = 14
WH_KEYBOARD_LL = 13


@pytest.fixture
def hook_calls(monkeypatch):
    """Record every SetWindowsHookExW call instead of installing a real hook."""
    calls: list[tuple] = []

    def record(*args):
        calls.append(args)
        return 0

    monkeypatch.setattr(control_win32._user32, "SetWindowsHookExW", record)
    monkeypatch.setattr(control_win32._user32, "UnhookWindowsHookEx", lambda handle: True)
    return calls


@pytest.mark.asyncio
async def test_no_control_installs_no_hooks(monkeypatch, hook_calls):
    """The whole server lifespan runs with control disabled and hooks nobody installs."""
    owner = control.ControlCoordinator()
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "false")
    monkeypatch.setattr(cli, "_mcp", None)
    monkeypatch.setattr(cli, "_control_notifier", None)
    monkeypatch.setattr(cli, "desktop", None)
    monkeypatch.setattr(cli, "_control_config", ControlConfig(enabled=False))
    monkeypatch.setattr(cli, "_start_watchdog", lambda desktop: None)
    monkeypatch.setattr(service, "Desktop", lambda: Mock(get_screen_size=lambda: (100, 100)))
    monkeypatch.setattr(control, "get_controller", lambda: owner)
    monkeypatch.setattr(control_status, "get_controller", lambda: owner)
    monkeypatch.setattr(control_overlay, "start", Mock(side_effect=AssertionError))
    monkeypatch.setattr(control_overlay, "stop", Mock(side_effect=AssertionError))
    monkeypatch.setattr(ControlNotifier, "start", Mock(side_effect=AssertionError))

    mcp = cli._build_mcp()
    async with mcp._lifespan(mcp):
        assert owner.enabled is False
        # The input ledger still runs — it is what lets injected input through.
        assert hook_calls == []

    assert hook_calls == []


def test_the_probe_sees_a_hook_install(hook_calls):
    """Positive control: the same probe catches the install it is meant to forbid.

    Without this, a probe that never fires would make the test above pass for the
    wrong reason.
    """
    owner = control.ControlCoordinator()

    with pytest.raises(OSError):
        # record() returns 0 for both hooks, so _install_hooks reports the failure —
        # the point here is that the calls were observed.
        owner._install_hooks(None)

    assert [args[0] for args in hook_calls] == [WH_MOUSE_LL, WH_KEYBOARD_LL]


def test_disabled_controller_start_installs_no_hooks(hook_calls):
    """`start()` on a disabled coordinator returns before any monitor thread."""
    owner = control.ControlCoordinator()
    owner.enabled = False

    owner.start()

    assert hook_calls == []
    assert owner._thread is None


def test_cli_no_control_disables_the_controller(monkeypatch, tmp_path):
    """The flag is what reaches the gate the hooks hang off."""
    seen: list[ControlConfig] = []
    monkeypatch.setattr(cli, "_control_config", ControlConfig())
    monkeypatch.setattr(cli, "install_selfpipe_guard", lambda: None)
    monkeypatch.setattr(cli.asyncio, "set_event_loop_policy", lambda _: None)
    monkeypatch.setattr(cli, "_run_server", lambda **kw: seen.append(cli._control_config))

    result = CliRunner().invoke(
        cli.main, ["serve", "--no-control"], env={"WINDOWS_MCP_CONTROL": None}
    )

    assert result.exit_code == 0, result.output
    assert seen[0].enabled is False
