"""`--tree-timeout` publishes the capture deadline the Tree reads per capture."""

import os

import pytest
from click.testing import CliRunner

from windows_mcp import __main__ as cli
from windows_mcp.tree.budget import DEFAULT_TREE_TIMEOUT_SEC, resolve_tree_timeout


@pytest.fixture
def invoke(monkeypatch):
    monkeypatch.delenv("WINDOWS_MCP_TREE_TIMEOUT", raising=False)
    monkeypatch.setattr(cli, "install_selfpipe_guard", lambda: None)
    monkeypatch.setattr(cli.asyncio, "set_event_loop_policy", lambda _: None)
    seen: list[float] = []
    monkeypatch.setattr(cli, "_run_server", lambda **kw: seen.append(resolve_tree_timeout()))

    def run(args=()):
        result = CliRunner().invoke(cli.main, ["serve", *args])
        return result, seen

    yield run
    # serve() publishes the flag by writing os.environ itself, which monkeypatch
    # does not track — drop it so the value cannot leak into later tests.
    os.environ.pop("WINDOWS_MCP_TREE_TIMEOUT", None)


def test_flag_sets_the_capture_deadline(invoke):
    result, seen = invoke(["--tree-timeout", "2.5"])
    assert result.exit_code == 0, result.output
    assert seen == [2.5]


def test_without_the_flag_the_default_applies(invoke):
    result, seen = invoke()
    assert result.exit_code == 0, result.output
    assert seen == [DEFAULT_TREE_TIMEOUT_SEC]


def test_flag_overrides_the_environment(invoke, monkeypatch):
    monkeypatch.setenv("WINDOWS_MCP_TREE_TIMEOUT", "60")
    result, seen = invoke(["--tree-timeout", "3"])
    assert result.exit_code == 0, result.output
    assert seen == [3.0]


def test_environment_alone_is_honoured(invoke, monkeypatch):
    monkeypatch.setenv("WINDOWS_MCP_TREE_TIMEOUT", "45")
    result, seen = invoke()
    assert result.exit_code == 0, result.output
    assert seen == [45.0]


def test_invalid_environment_value_falls_back_to_default_without_failing(invoke, monkeypatch):
    """An unusable env value must not stop the server — same contract as the element limit."""
    monkeypatch.setenv("WINDOWS_MCP_TREE_TIMEOUT", "garbage")
    result, seen = invoke()
    assert result.exit_code == 0, result.output
    assert seen == [DEFAULT_TREE_TIMEOUT_SEC]


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_invalid_flag_never_starts_the_server(invoke, value):
    result, seen = invoke(["--tree-timeout", value])
    assert result.exit_code != 0
    assert seen == []


def test_flag_is_listed_in_serve_help():
    result = CliRunner().invoke(cli.main, ["serve", "--help"])
    assert result.exit_code == 0
    assert "--tree-timeout" in result.output


def test_flag_before_the_subcommand_is_caught_as_a_legacy_flag():
    result = CliRunner().invoke(cli.main, ["--tree-timeout", "5"])
    assert result.exit_code != 0
    assert "windows-mcp serve" in result.output
