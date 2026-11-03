"""qcl is the only console command the package installs."""
import shlex
import sys
from importlib.metadata import entry_points

import pytest

from context_lake import cli


def test_qcl_is_the_only_command():
    scripts = {ep.name: ep.value for ep in entry_points(group='console_scripts')
               if ep.value.startswith('context_lake.')}
    assert scripts == {'qcl': 'context_lake.cli:main'}


def test_missing_extra_hint_targets_the_running_interpreter(monkeypatch, capsys):
    def missing(name, package=None):
        raise ModuleNotFoundError("No module named 'boto3'", name='boto3')

    monkeypatch.setattr(cli.importlib, 'import_module', missing)
    with pytest.raises(SystemExit):
        cli.main(['survey', 's3://bucket/prefix'])
    message = capsys.readouterr().err
    interpreter = shlex.quote(sys.executable)
    assert f"uv pip install --python {interpreter} 'quilt-context-lake[s3]'" in message
    assert "uv tool install 'quilt-context-lake[s3]'" in message
