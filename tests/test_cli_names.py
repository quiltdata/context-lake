"""The short and verbose command names must stay aliases of one entry point."""
from importlib.metadata import entry_points


def test_clake_and_context_lake_share_the_cli_entry_point():
    scripts = {ep.name: ep.value for ep in entry_points(group='console_scripts')
               if ep.value.startswith('context_lake.')}
    assert scripts.get('clake') == 'context_lake.cli:main'
    assert scripts.get('context-lake') == 'context_lake.cli:main'
