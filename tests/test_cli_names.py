"""qcl is the only console command the package installs."""
from importlib.metadata import entry_points


def test_qcl_is_the_only_command():
    scripts = {ep.name: ep.value for ep in entry_points(group='console_scripts')
               if ep.value.startswith('context_lake.')}
    assert scripts == {'qcl': 'context_lake.cli:main'}
