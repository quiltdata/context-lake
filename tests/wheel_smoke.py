"""Run with an isolated Python containing only the installed core wheel."""
import json
import sys
import tempfile
from importlib.resources import files
from pathlib import Path

from context_lake.cli import main
from context_lake.email_threads_to_markdown import convert

assert files('context_lake').joinpath('schemas/output.schema.json').is_file()
assert files('context_lake').joinpath('prompts/summarizer.md').is_file()
assert files('context_lake').joinpath('model_rates.json').is_file()
with tempfile.TemporaryDirectory() as directory:
    source = Path(directory) / 'source'
    source.mkdir()
    (source / 'example.md').write_text('Exact source text.')
    output = Path(directory) / 'output'
    assert main(['sync', str(source), str(output), '--source-id', 'example']) == 0
    assert not output.exists()
    assert main(['sync', str(source), str(output), '--source-id', 'example', '--apply']) == 0
    state = json.loads((output / 'CURRENT.json').read_text())
    assert (output / 'revisions' / state['revision'] / 'files/example.md').read_text() == 'Exact source text.'
    thread = {'id': 't1', 'messages': [{'id': 'm1', 'payload': {
        'mime_type': 'text/plain', 'headers': {'Subject': 'Example'},
        'body': {'content': 'Exact source text.'}}}]}
    assert 'Exact source text.' in convert(thread)['t1']
assert 'context_lake.run_model' not in sys.modules
assert 'boto3' not in sys.modules and 'quilt3' not in sys.modules
print('Installed core wheel works outside the checkout with no model/storage dependencies.')
