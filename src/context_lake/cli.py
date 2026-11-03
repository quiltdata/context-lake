"""Command interface; deterministic operations never invoke model adapters."""
import argparse
import importlib
import shlex
import sys

from . import __version__

COMMANDS = {
    'sync': ('sync', 'Reconcile a local source into versioned context snapshots'),
    'email': ('email_threads_to_markdown', 'Normalize saved Gmail threads to Markdown'),
    'fathom': ('fathom_to_markdown', 'Export approved Fathom recordings to Markdown'),
    'index': ('transcript_index', 'Build a deterministic transcript index'),
    'backfill': ('backfill_transcripts', 'Repair selected transcript placeholders'),
    'survey': ('survey', 'Inventory S3 objects without model calls'),
    'extract': ('make_inputs', 'Extract source text and bounded table samples'),
    'publish': ('publish', 'Publish an approved Quilt plan; dry run by default'),
    'verify': ('check_outputs', 'Validate enrichment rows and source quotes'),
    'enrich': ('run_model', 'OPTIONAL: call a model to enrich extracted sources'),
}
EXTRAS = {'boto3': 's3', 'botocore': 's3', 'quilt3': 'quilt',
          'pypdf': 'formats', 'pyarrow': 'formats', 'openpyxl': 'formats'}


def missing_extra_hint(extra):
    """Name commands that add the extra to the environment running this CLI."""
    package = shlex.quote(f'quilt-context-lake[{extra}]')
    return (f"this operation requires the '{extra}' extra.\n"
            f"  Install it into this environment:\n"
            f"    uv pip install --python {shlex.quote(sys.executable)} {package}\n"
            f"  Or, if qcl was installed with uv tool:\n"
            f"    uv tool install {package}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('command', choices=COMMANDS, nargs='?',
                        help='; '.join(f'{k}: {v[1]}' for k, v in COMMANDS.items()))
    if not argv or argv[0] in ('-h', '--help', '--version'):
        parser.parse_args(argv)
        if not argv:
            parser.print_help()
        return 0
    args = parser.parse_args(argv[:1])
    try:
        module = importlib.import_module('.' + COMMANDS[args.command][0], __package__)
        if args.command == 'verify':
            check = argparse.ArgumentParser(description=module.__doc__)
            check.add_argument('inputs')
            check.add_argument('outputs')
            values = check.parse_args(argv[1:])
            return module.main(values.inputs, values.outputs)
        return module.main(argv[1:])
    except ModuleNotFoundError as error:
        extra = EXTRAS.get(error.name)
        if not extra:
            raise
        parser.error(missing_extra_hint(extra))
