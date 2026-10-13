import pytest
from typer.core import TyperGroup
from typer.main import get_command
from typer.testing import CliRunner

from bront.cli import app
from bront.reference.config import Settings


def test_public_commands_match_the_release_workflow():
    command_group = get_command(app)
    assert isinstance(command_group, TyperGroup)
    assert set(command_group.commands) == {"run", "compare", "report"}
    for command in ("run", "compare", "report"):
        assert CliRunner().invoke(app, [command, "--help"]).exit_code == 0


@pytest.mark.parametrize("command", ["benchmark", "status", "fault"])
def test_retired_commands_are_not_silent_aliases(command):
    assert CliRunner().invoke(app, [command]).exit_code == 2


def test_reference_configuration_uses_bront_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRONT_FAILURE_THRESHOLD", "7")
    monkeypatch.setenv("BRONT_PRIMARY_URL", "http://renamed-primary:9000")
    settings = Settings()
    assert settings.failure_threshold == 7
    assert str(settings.primary_url) == "http://renamed-primary:9000/"
