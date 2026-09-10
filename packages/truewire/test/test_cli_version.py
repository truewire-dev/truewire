"""`truewire --version` is the first command a newcomer runs; it must answer."""

from importlib.metadata import version

from typer.testing import CliRunner

from truewire.cli import app


def test_version_prints_the_installed_version():
  result = CliRunner().invoke(app, ['--version'])
  assert result.exit_code == 0, result.output
  assert result.output.strip() == f'truewire {version("truewire")}'


def test_short_flag_matches():
  assert CliRunner().invoke(app, ['-V']).output == CliRunner().invoke(app, ['--version']).output


def test_no_arguments_still_prints_help():
  result = CliRunner().invoke(app, [])
  assert 'Usage' in result.output
