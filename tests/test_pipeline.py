"""Offline integration tests for the context-lake data pipeline."""
import csv
import importlib
import json
from argparse import Namespace
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'src/context_lake'


@pytest.fixture(autouse=True)
def isolated_quilt_config(tmp_path, monkeypatch):
    import quilt3.util
    import quilt3.session
    monkeypatch.setattr(quilt3.session, "_load_credentials", lambda: None)
    config = tmp_path / 'quilt-config.yml'
    config.write_text('telemetry_disabled: true\ndefault_registry_version: 1\n')
    monkeypatch.setattr(quilt3.util, 'CONFIG_PATH', config)
    monkeypatch.setattr(quilt3.util, 'IS_CACHE_ENABLED', False)


def load(name, path):
    return importlib.reload(importlib.import_module('context_lake.' + path.stem))


@pytest.fixture
def helpers(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS.parent))
    return {name: load('context_' + name, SCRIPTS / (name + '.py'))
            for name in ['survey', 'make_inputs', 'run_model', 'check_outputs', 'publish']}


def test_whole_text_and_kind_sampling_preserve_sources(helpers):
    extract = helpers['make_inputs']
    with mock_aws():
        s3 = boto3.client('s3', region_name='us-east-1')
        s3.create_bucket(Bucket='test-corpus')
        for n in (1, 2):
            s3.put_object(Bucket='test-corpus', Key=f'papers/paper{n}.txt',
                          Body=b'An assay measured protein abundance in human cells.')
        objects = list(helpers['survey'].list_s3('test-corpus', 'papers/'))
        _, units = helpers['survey'].survey(objects, 'papers/', 1)
        assert units == [('papers/', 'paper[0-9].txt', 2, True)]
        extract.s3 = s3
        extract.EXCERPT_CHARS = 0
        row = extract.row_for('s3://test-corpus/papers/paper*.txt')
        assert row['members'] == 2
        assert row['excerpt'].startswith('An assay measured')
        assert 'one sample of 2' in row['excerpt_method']
        assert len(list(s3.list_objects_v2(Bucket='test-corpus')['Contents'])) == 2

def test_model_failure_unknown_cost_and_retry(helpers):
    runner = helpers['run_model']
    class Partial:
        def call(self, rows):
            return [], {'cost_usd': None, 'input_tokens': 12}
    record, output = runner.call(Partial(), Namespace(model='unknown'), 0,
                                 ('prefix', 'txt'), [{'unit_id': 'one', 'excerpt': 'Source text.'}])
    assert record['attempts'] == 2
    assert record['error']
    assert record['cost_usd'] is None
    assert record['input_tokens'] == 24
    assert output == []

def test_quote_validation_rejects_missing_invented_and_duplicate_rows(helpers, tmp_path):
    check = helpers['check_outputs']
    inputs = tmp_path / 'inputs.csv'
    outputs = tmp_path / 'outputs.csv'
    with inputs.open('w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=['unit_id', 'excerpt', 'mentions'])
        w.writeheader()
        w.writerow({'unit_id': 'one', 'excerpt': 'Human cells were imaged.', 'mentions': '[]'})
    valid = dict(unit_id='one', what='article', summary='An imaging study.', about='[]',
                 entities='[]', refers_to='[]', evidence='["Human cells were imaged."]')
    for rows, code in [([valid], 0), ([], 1), ([valid, valid], 1),
                       ([dict(valid, evidence='["Invented quote"]')], 1),
                       ([dict(valid, evidence='null')], 1), ([dict(valid, evidence='[123]')], 1)]:
        with outputs.open('w', newline='') as fh:
            w = csv.DictWriter(fh, fieldnames=list(valid))
            w.writeheader()
            w.writerows(rows)
        with pytest.raises(SystemExit) as exc:
            check.main(inputs, outputs)
        assert exc.value.code == code

def test_plan_validates_all_rows_before_publish(helpers, tmp_path):
    export = tmp_path / 'export'
    export.mkdir()
    plan = tmp_path / 'plan.csv'
    plan.write_text('name,source\ncontext/notes,export\ninvalid,export\n')
    with pytest.raises(ValueError, match='invalid'):
        helpers['publish'].read_plan(plan)
    plan.write_text('name,source\ncontext/notes,export\n')
    assert helpers['publish'].read_plan(plan)[0]['source'] == str(export)

def test_quilt_publish_by_reference_and_local_upload(helpers, tmp_path, monkeypatch):
    import quilt3
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
    with mock_aws():
        s3 = boto3.client('s3', region_name='us-east-1')
        for bucket in ['test-corpus', 'test-lake']:
            s3.create_bucket(Bucket=bucket)
        s3.put_object(Bucket='test-corpus', Key='dataset/data.txt', Body=b'original')
        readme = tmp_path / 'README.md'
        readme.write_text('# A dataset\n')
        pkg = helpers['publish'].build({'source': 's3://test-corpus/dataset/', 'readme': str(readme)})
        published = pkg.push('corpus/assays', registry='s3://test-lake',
                             selector_fn=quilt3.Package.selector_fn_copy_local)
        assert published['data.txt'].physical_key.bucket == 'test-corpus'
        assert published['README.md'].physical_key.bucket == 'test-lake'
        assert s3.get_object(Bucket='test-corpus', Key='dataset/data.txt')['Body'].read() == b'original'
        assert not any(o['Key'].endswith('data.txt')
                       for o in s3.list_objects_v2(Bucket='test-lake')['Contents'])
        assert quilt3.Package.browse('corpus/assays', registry='s3://test-lake').top_hash == published.top_hash

def test_model_retries_unverified_quotes_before_success(helpers):
    runner = helpers['run_model']
    class CorrectOnRetry:
        attempts = 0

        def call(self, rows):
            self.attempts += 1
            evidence = 'invented' if self.attempts == 1 else 'Source text.'
            row = dict(unit_id='one', what='article', summary='An article.', about=[],
                       entities=[], refers_to=[], evidence=[evidence])
            return [row], {'cost_usd': 0.001, 'input_tokens': 12}
    record, output = runner.call(CorrectOnRetry(), Namespace(model='test'), 0,
                                 ('prefix', 'txt'), [{'unit_id': 'one', 'excerpt': 'Source text.'}])
    assert not record['error']
    assert record['attempts'] == 2
    assert record['cost_usd'] == 0.002
    assert output[0]['evidence'] == ['Source text.']

def test_approved_numeric_group_cannot_sample_excluded_file(helpers):
    extract = helpers['make_inputs']
    with mock_aws():
        s3 = boto3.client('s3', region_name='us-east-1')
        s3.create_bucket(Bucket='test-corpus')
        for name in ['sample.txt', 'sample1.txt', 'sample2.txt', 'sample-secret.txt']:
            s3.put_object(Bucket='test-corpus', Key='data/' + name,
                          Body=('Raw content for ' + name).encode())
        objects = list(helpers['survey'].list_s3('test-corpus', 'data/'))
        _, units = helpers['survey'].survey(objects, 'data/', 1)
        approved = next(u for u in units if u[2] == 2)
        extract.s3 = s3
        row = extract.row_for('s3://test-corpus/' + approved[0] + approved[1])
        assert row['members'] == 2
        assert json.loads(row['example_keys']) == ['sample1.txt', 'sample2.txt']
        assert row['excerpt'] == 'Raw content for sample1.txt'

@pytest.mark.parametrize('name,function', [('bedrock_tool', 'call_tool'), ('bedrock_chat', 'call_json')])
def test_standalone_model_cli_reports_failure(name, function, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS.parent))
    module = load(name + '_cli', SCRIPTS / (name + '.py'))
    for file, text in [('system.md', 'instructions'), ('user.txt', 'content'), ('schema.json', '{}')]:
        (tmp_path / file).write_text(text)
    monkeypatch.setattr(module, function, lambda *a, **kw: (None, {'error': 'AccessDenied'}))
    monkeypatch.setattr('sys.argv', [name, '--model', 'test', '--region', 'us-east-1',
                                    '--system', str(tmp_path / 'system.md'),
                                    '--user', str(tmp_path / 'user.txt'),
                                    '--schema', str(tmp_path / 'schema.json')])
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 1

def test_extraction_partial_failure_keeps_rows_and_fails(helpers, tmp_path, monkeypatch):
    extract = helpers['make_inputs']
    objects = tmp_path / 'objects.txt'
    output = tmp_path / 'inputs.csv'
    objects.write_text('s3://source/good.txt\ns3://source/missing.txt\n')
    def fake_row(uri):
        if uri.endswith('missing.txt'):
            raise ValueError('missing source')
        return dict(unit_id=uri, prefix='s3://source/', unit='good.txt', file_type='txt',
                    members=1, example_keys='["good.txt"]', size_bytes=30,
                    excerpt_method='full text', excerpt='The measured assay result.', mentions='[]')
    monkeypatch.setattr(extract, 'row_for', fake_row)
    monkeypatch.setattr(boto3, 'client', lambda *a, **kw: object())
    monkeypatch.setattr('sys.argv', ['make_inputs', str(objects), str(output)])
    with pytest.raises(SystemExit) as exc:
        extract.main()
    assert exc.value.code == 1
    with output.open() as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1 and rows[0]['unit_id'].endswith('good.txt')

def test_publication_cli_dry_run_and_write_log(helpers, tmp_path, monkeypatch):
    publisher = helpers['publish']
    export = tmp_path / 'export'
    export.mkdir()
    plan = tmp_path / 'plan.csv'
    plan.write_text('name,source\ncontext/notes,export\n')
    calls = []
    class Package:
        def push(self, name, **kwargs):
            calls.append((name, kwargs))
            return Namespace(top_hash='a' * 64)
    monkeypatch.setattr(publisher, 'build', lambda row: Package())
    argv = ['publish', str(plan), '--registry', 's3://test-lake', '--log', str(tmp_path / 'log.jsonl')]
    monkeypatch.setattr('sys.argv', argv)
    publisher.main()
    assert calls == [] and not (tmp_path / 'log.jsonl').exists()
    monkeypatch.setattr('sys.argv', argv + ['--yes'])
    publisher.main()
    assert calls[0][0] == 'context/notes'
    assert json.loads((tmp_path / 'log.jsonl').read_text())['top_hash'] == 'a' * 64

def test_converse_falls_back_from_forced_tool_to_text(helpers):
    runner = helpers['run_model']
    class ValidationError(Exception):
        pass
    class Client:
        exceptions = Namespace(ValidationException=ValidationError)
        requests = []
        def converse(self, **kwargs):
            self.requests.append(kwargs)
            if 'toolConfig' in kwargs:
                raise ValidationError('tool not supported')
            return {'usage': {'inputTokens': 5, 'outputTokens': 3},
                    'output': {'message': {'content': [{'text': '{"rows": []}'}]}}}
    backend = object.__new__(runner.Bedrock)
    backend.args = Namespace(model='unknown')
    backend.client = Client()
    backend.mode = 'tool'
    backend.max_tokens = 1000
    rows, record = backend.call([])
    assert rows == [] and record['mode'] == 'text' and record['cost_usd'] is None
    assert len(backend.client.requests) == 3
    assert backend.client.requests[0]['toolConfig']['toolChoice'] == {'tool': {'name': 'record_rows'}}
    assert backend.client.requests[1]['toolConfig']['toolChoice'] == {'auto': {}}

@pytest.mark.parametrize('literal,other', [('sample[1].txt', 'sample1.txt'),
                                          ('sample*.txt', 'sample-secret.txt'),
                                          ('sample?.txt', 'sample1.txt')])
def test_approved_literal_filename_cannot_expand_as_glob(helpers, literal, other):
    extract = helpers['make_inputs']
    with mock_aws():
        s3 = boto3.client('s3', region_name='us-east-1')
        s3.create_bucket(Bucket='test-corpus')
        for name in [literal, other]:
            s3.put_object(Bucket='test-corpus', Key='data/' + name,
                          Body=('Raw content for ' + name).encode())
        objects = list(helpers['survey'].list_s3('test-corpus', 'data/'))
        _, units = helpers['survey'].survey(objects, 'data/', 1)
        import fnmatch
        approved = next(u for u in units if fnmatch.fnmatchcase(literal, u[1]))
        extract.s3 = s3
        row = extract.row_for('s3://test-corpus/' + approved[0] + approved[1])
        assert row['members'] == 1
        assert json.loads(row['example_keys']) == [literal]
        assert row['excerpt'] == 'Raw content for ' + literal

@pytest.mark.parametrize('quote,source', [('-20 °C', '20 °C'), ('20 °C', '-20 °C'),
                                        ('1.5 mM', '15 mM'), ('Dose: 10 mg', 'Dose: 1.0 mg'),
                                        ('p > 0.05', 'p < 0.05'), ('1 M', '1 m'),
                                        ('5 mM', '1.5 mM'), ('20', '200')])
def test_quote_verification_preserves_measurements(helpers, quote, source):
    assert not helpers['check_outputs'].quote_found(quote, source)

def test_quote_verification_normalizes_only_wrapping_and_typography(helpers):
    found = helpers['check_outputs'].quote_found
    assert found('methods at −20 °C', 'meth-\nods at -20 °C')
    assert found('Dose: 1.5 mM', 'Dose:   1.5\nmM')
    assert not found('methods', 'meth-ods')

def test_model_retry_preserves_verified_rows_and_only_resends_missing(helpers):
    runner = helpers['run_model']
    class Partial:
        requests = []
        def call(self, rows):
            self.requests.append([r['unit_id'] for r in rows])
            uid = 'A' if len(self.requests) == 1 else 'B'
            return [dict(unit_id=uid, what='article', summary='An article.', about=[],
                         entities=[], refers_to=[], evidence=['Source text.'])], {'cost_usd': 0.001}
    backend = Partial()
    record, output = runner.call(backend, Namespace(model='test'), 0, ('p', 'txt'),
                                 [{'unit_id': uid, 'excerpt': 'Source text.'} for uid in ['A', 'B']])
    assert not record['error'] and record['cost_usd'] == 0.002
    assert [r['unit_id'] for r in output] == ['A', 'B']
    assert backend.requests == [['A', 'B'], ['B']]

def test_vcf_complete_last_record_and_uncapped_headers(helpers, monkeypatch):
    extract = helpers['make_inputs']
    content = b'##fileformat=VCFv4.2\n##source=Test\n#CHROM POS REF ALT\n1 99 A T\n'
    monkeypatch.setattr(extract, 'get', lambda *a: content)
    extract.EXCERPT_CHARS = 0
    _, text = extract.excerpt('bucket', 'data.vcf', len(content))
    assert '##fileformat=VCFv4.2' in text
    assert '##source=Test' in text and '1 99 A T' in text
    monkeypatch.setattr(extract, 'get', lambda *a: content + b'2 incomplete')
    _, text = extract.excerpt('bucket', 'data.vcf', 300000)
    assert '1 99 A T' in text and '2 incomplete' not in text

def test_folder_inventory_reused_and_unapproved_signposts_unread(helpers):
    extract = helpers['make_inputs']
    with mock_aws():
        s3 = boto3.client('s3', region_name='us-east-1')
        s3.create_bucket(Bucket='test-corpus')
        for key in ['data/first.txt', 'data/second.txt', 'data/README.md', 'README.md']:
            s3.put_object(Bucket='test-corpus', Key=key, Body=b'first.txt and second.txt experimental observations.')
        calls = []
        s3.meta.events.register('before-parameter-build.s3', lambda params, model, **kw: calls.append((model.name, dict(params))))
        extract.s3 = s3
        first = extract.row_for('s3://test-corpus/data/first.txt')
        second = extract.row_for('s3://test-corpus/data/second.txt')
        assert first['mentions'] == second['mentions'] == '[]'
        assert len([c for c in calls if c[0] == 'ListObjectsV2']) == 1
        assert {params['Key'] for name, params in calls if name == 'GetObject'} == {'data/first.txt', 'data/second.txt'}
        extract.APPROVED_SIGNPOSTS = frozenset({('test-corpus', 'data/README.md')})
        extract._signposts.clear()
        with_mentions = extract.row_for('s3://test-corpus/data/first.txt')
        assert 'data/README.md' in with_mentions['mentions']
        assert not any(params.get('Key') == 'README.md' for _, params in calls)

def test_model_batches_bound_rows_and_serialized_input(helpers):
    runner = helpers['run_model']
    rows = [batch_row(str(i)) for i in range(9)]
    work = runner.batches(rows, max_rows=4)
    assert [len(group) for _, group in work] == [4, 4, 1]
    budget = len(runner.user_message(rows[:2]))
    work = runner.batches(rows, max_rows=4, max_chars=budget)
    assert all(len(group) <= 2 and len(runner.user_message(group)) <= budget for _, group in work)
    with pytest.raises(ValueError, match='shorten the excerpt'):
        runner.batches([batch_row('huge', 'x' * 25000)])

def test_oversized_input_rejected_before_backend_creation(helpers, tmp_path, monkeypatch):
    runner = helpers['run_model']
    path = tmp_path / 'input.csv'
    row = batch_row('huge', 'x' * 25000)
    with path.open('w', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    def unexpected_backend(args):
        pytest.fail('oversized requests must be rejected before creating the backend')
    monkeypatch.setitem(runner.BACKENDS, 'openai', unexpected_backend)
    monkeypatch.setattr('sys.argv', ['run_model', str(path), str(tmp_path / 'out'),
                                    '--backend', 'openai', '--base-url', 'http://localhost/v1',
                                    '--model', 'test', '--limit', '1'])
    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 2

@pytest.fixture
def fathom_parser():
    return load('fathom_parser', SCRIPTS / 'fathom_to_markdown.py')

def test_fathom_api_import_preserves_source_and_raw_response(fathom_parser, tmp_path, monkeypatch):
    parser = fathom_parser
    segments = [
        {'timestamp': '00:00:01', 'speaker': {'display_name': 'Analyst'}, 'text': 'Dose: 1.5 mM.'},
        {'timestamp': '00:00:03', 'speaker': {'display_name': 'Analyst'}, 'text': 'Measured at −20 °C.'},
    ]
    raw = json.dumps({'transcript': segments}, ensure_ascii=False).encode()
    meeting = {'recording_id': 123, 'title': 'Example meeting',
               'recording_start_time': '2026-10-28T18:00:00Z',
               'url': 'https://fathom.video/calls/456'}
    class Response:
        def __init__(self, data):
            self.data = data
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return self.data
    requested = []
    def opener(request, **kwargs):
        requested.append(request.full_url)
        if '/meetings?' in request.full_url:
            return Response(json.dumps({'items': [meeting]}).encode())
        assert request.full_url.endswith('/recordings/123/transcript')
        return Response(raw)
    monkeypatch.setattr(parser, 'DEFAULT_OPENER', opener)
    monkeypatch.setenv('FATHOM_API_KEY', 'synthetic-test-key')
    export = tmp_path / 'export'
    raw_path = tmp_path / 'raw.json'
    assert parser.main(['--recording-id', '123', '--output-dir', str(export),
                        '--save-json', str(raw_path)]) == 0
    output = export / 'transcripts/2026-10-28--example-meeting--123.md'
    text = output.read_text()
    assert 'recording_id: 123' in text and 'source_url: "https://fathom.video/calls/456"' in text
    assert '[00:01](https://fathom.video/calls/456?timestamp=1) Analyst: Dose: 1.5 mM. Measured at −20 °C.' in text
    assert raw_path.read_bytes() == raw
    assert 'synthetic-test-key' not in text
    assert len(requested) == 2
    separate = parser.transcript_from_api_segments(segments, meeting['url'], coalesce=False)
    assert separate.count('Analyst:') == 2 and '?timestamp=3' in separate

def test_fathom_offline_render_and_overwrite_protection(fathom_parser, tmp_path, monkeypatch):
    parser = fathom_parser
    monkeypatch.delenv('FATHOM_API_KEY', raising=False)
    body = tmp_path / 'body.txt'
    body.write_text('[00:01](https://fathom.video/calls/456?timestamp=1) Analyst: Exact words.\n')
    argv = ['--input', str(body), '--recording-id', '123', '--title', 'Example meeting',
            '--date', '2026-10-28', '--source-url', 'https://fathom.video/calls/456',
            '--output-dir', str(tmp_path / 'export')]
    assert parser.main(argv) == 0
    output = tmp_path / 'export/transcripts/2026-10-28--example-meeting--123.md'
    before = output.read_bytes()
    with pytest.raises(SystemExit) as exc:
        parser.main(argv)
    assert exc.value.code == 2 and output.read_bytes() == before

@pytest.mark.parametrize('body', [
    '[00:01](https://fathom.video/calls/999?timestamp=1) Analyst: Wrong call.',
    '[00:01](https://fathom.video/calls/456?timestamp=2) Analyst: Wrong time.',
    '[00:02](https://fathom.video/calls/456?timestamp=2) Analyst: First.\n'
    '[00:01](https://fathom.video/calls/456?timestamp=1) Analyst: Out of order.',
])
def test_fathom_rejects_inconsistent_transcript(fathom_parser, body):
    with pytest.raises(fathom_parser.TranscriptFormatError):
        fathom_parser.render_markdown('Example meeting', '2026-10-28', 123,
                                      'https://fathom.video/calls/456', body)

@pytest.mark.parametrize('altered,source', [('106 cells', '10⁶ cells'), ('H2O', 'H₂O'),
                                          ('2 mg', '² mg'), ('1 mM', '① mM')])
def test_quote_verification_preserves_unicode_numeric_forms(helpers, altered, source):
    found = helpers['check_outputs'].quote_found
    assert not found(altered, source)
    assert found(source, source)
    assert found('café', 'cafe\u0301')  # canonical Unicode equivalence remains valid

@pytest.mark.parametrize('complete', [True, False])
def test_unhashable_model_ids_do_not_discard_verified_rows(helpers, complete):
    class Backend:
        requests = []
        def call(self, rows):
            self.requests.append([row['unit_id'] for row in rows])
            uid = 'A' if len(self.requests) == 1 else 'B'
            valid = dict(unit_id=uid, what='article', summary='An article.', about=[],
                         entities=[], refers_to=[], evidence=['Source text.'])
            output = [valid] if uid == 'A' or complete else []
            return output + [{'unit_id': []}, {'unit_id': {}}, {'unit_id': 7}], {'cost_usd': 0.001}
    backend = Backend()
    record, output = helpers['run_model'].call(backend, Namespace(model='test'), 0,
        ('p', 'txt'), [{'unit_id': uid, 'excerpt': 'Source text.'} for uid in ['A', 'B']])
    assert [row['unit_id'] for row in output] == (['A', 'B'] if complete else ['A'])
    assert bool(record['error']) == (not complete)
    assert record['cost_usd'] == 0.002
    assert backend.requests == [['A', 'B'], ['B']]

@pytest.mark.parametrize('row_count', [0, 1000])
def test_parquet_excerpt_uses_metadata_and_one_bounded_batch(helpers, monkeypatch, row_count):
    import io
    import pyarrow as pa
    import pyarrow.parquet as pq
    table = pa.table({'number': list(range(row_count)), 'label': ['sample'] * row_count})
    buffer = io.BytesIO()
    pq.write_table(table, buffer, row_group_size=200, compression='gzip')
    data = buffer.getvalue()
    extract = helpers['make_inputs']
    monkeypatch.setattr(extract, 'get', lambda *a, **kw: data)
    original = pq.ParquetFile
    requests = []
    class BoundedParquet:
        def __init__(self, source):
            self.file = original(source)
            self.schema_arrow = self.file.schema_arrow
            self.metadata = self.file.metadata
        def iter_batches(self, *, batch_size):
            requests.append(batch_size)
            first = next(self.file.iter_batches(batch_size=batch_size), None)
            if first is None:
                return
            yield first
            pytest.fail('excerpt consumed more than one batch')
    monkeypatch.setattr(pq, 'ParquetFile', BoundedParquet)
    monkeypatch.setattr(pq, 'read_table', lambda *a, **kw: pytest.fail('full Parquet decode'))
    method, text = extract.excerpt('bucket', 'data.parquet', len(data))
    assert f'{row_count:,} rows; 2 columns' in method
    rows = list(csv.DictReader(io.StringIO(text.split('first rows:\n', 1)[1])))
    assert len(rows) == min(extract.TABLE_ROWS, row_count)
    assert requests == [extract.TABLE_ROWS]
    if rows:
        assert [r['number'] for r in rows] == ['0', '1', '2', '3', '4']

def test_escaped_singleton_uses_actual_filename_in_approved_signposts(helpers):
    extract = helpers['make_inputs']
    with mock_aws():
        s3 = boto3.client('s3', region_name='us-east-1')
        s3.create_bucket(Bucket='test-corpus')
        s3.put_object(Bucket='test-corpus', Key='data/sample[1].txt', Body=b'The assay measured protein abundance.')
        s3.put_object(Bucket='test-corpus', Key='data/README.md', Body=b'sample[1].txt contains assay measurements.')
        extract.s3 = s3
        extract.APPROVED_SIGNPOSTS = frozenset({('test-corpus', 'data/README.md')})
        row = extract.row_for('s3://test-corpus/data/sample[[]1].txt')
        assert row['members'] == 1
        assert 'sample[1].txt contains assay measurements.' in row['mentions']


def batch_row(uid, excerpt='A short source excerpt.'):
    return dict(unit_id=uid, prefix='p', file_type='txt', members='1', size_bytes='50',
                example_keys='[]', mentions='[]', excerpt=excerpt)


@pytest.mark.parametrize('prefix', ['data/sample1', 'data/sample[1]'])
def test_filename_prefix_never_samples_other_parent_objects(helpers, prefix):
    with mock_aws():
        s3 = boto3.client('s3', region_name='us-east-1')
        s3.create_bucket(Bucket='test-corpus')
        for name in ['data/sample00.txt', prefix + '0.txt', prefix + '1.txt']:
            s3.put_object(Bucket='test-corpus', Key=name, Body=(name + ' Assay measured protein abundance in human cells.').encode())
        survey = helpers['survey']
        _, units = survey.survey(list(survey.list_s3('test-corpus', prefix)), prefix, 1)
        assert len(units) == 2
        extract = helpers['make_inputs']
        extract.s3 = s3
        for folder, pattern, count, _ in units:
            row = extract.row_for('s3://test-corpus/' + folder + pattern)
            assert count == row['members'] == 1
            assert row['excerpt'].startswith(prefix) and 'sample00' not in row['excerpt']


@pytest.mark.parametrize('newline', ['\n', '\r'])
def test_survey_rejects_line_injection_before_output(helpers, tmp_path, monkeypatch, newline):
    survey = helpers['survey']
    out = tmp_path / 'scope.md'
    objects = tmp_path / 'objects.txt'
    out.write_text('original scope')
    objects.write_text('original objects')
    malicious = 'data/file.txt' + newline + 's3://other-bucket/private.txt'
    monkeypatch.setattr(survey, 'list_s3', lambda *args: [(malicious, 10)])
    monkeypatch.setattr('sys.argv', ['survey.py', 's3://test-corpus/data/', '--out', str(out),
                                   '--objects', str(objects)])
    with pytest.raises(SystemExit) as exc:
        survey.main()
    assert exc.value.code == 2
    assert out.read_text() == 'original scope' and objects.read_text() == 'original objects'
    with pytest.raises(ValueError, match='CR/LF'):
        survey.split_uri('s3://test-corpus/data/' + newline + 's3://other/private')


@pytest.mark.parametrize('adapter', ['bedrock', 'chat'])
@pytest.mark.parametrize('recover', [True, False])
def test_billed_invalid_json_keeps_usage_on_retry(helpers, monkeypatch, adapter, recover):
    runner = helpers['run_model']
    valid = dict(unit_id='A', what='article', summary='An article.', about=[],
                 entities=[], refers_to=[], evidence=['Source text.'])
    args = Namespace(model='test', region='us-east-1', backend='bedrock-chat', max_tokens=1000,
                     service_tier='default', reasoning_effort=None)
    count = 0
    if adapter == 'bedrock':
        from context_lake import rates
        monkeypatch.setattr(rates, 'usd', lambda *args: 0.001)
        class Client:
            def converse(self, **kwargs):
                nonlocal count
                count += 1
                text = json.dumps({'rows': [valid]}) if recover and count == 2 else '{"rows": ['
                return {'usage': {'inputTokens': 12, 'outputTokens': 3},
                        'stopReason': 'max_tokens', 'output': {'message': {'content': [{'text': text}]}}}
        backend = object.__new__(runner.Bedrock)
        backend.args, backend.client, backend.mode, backend.max_tokens = args, Client(), 'text', 1000
    else:
        from context_lake import bedrock_chat
        def fake_call(*args):
            nonlocal count
            count += 1
            ok = recover and count == 2
            return ({'rows': [valid]} if ok else None), {
                'input_tokens': 12, 'output_tokens': 3, 'cost_usd': 0.001,
                'error': '' if ok else 'no JSON object'}
        monkeypatch.setattr(bedrock_chat, 'call_json', fake_call)
        backend = runner.Chat(args)
    record, output = runner.call(backend, args, 0, ('data/', 'txt'),
                                 [batch_row('A', 'Source text.')])
    assert count == record['attempts'] == 2
    assert record['input_tokens'] == 24 and record['output_tokens'] == 6
    assert record['cost_usd'] == 0.002
    assert output == ([valid] if recover else [])
    assert bool(record['error']) is not recover


def test_docx_expanded_member_limit_prevents_decompression(helpers, monkeypatch):
    import io
    import zipfile
    extract = helpers['make_inputs']
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml', '<w:p><w:t>' + 'x' * 10000 + '</w:t></w:p>')
    raw = data.getvalue()
    monkeypatch.setattr(extract, 'get', lambda *args: raw)
    monkeypatch.setattr(extract, 'DOCX_XML_MAX_BYTES', 100)
    def forbidden(*args):
        pytest.fail('oversized member must fail before decompression')
    monkeypatch.setattr(zipfile.ZipFile, 'open', forbidden)
    with pytest.raises(ValueError, match='expanded-size'):
        extract.excerpt('bucket', 'archive.docx', len(raw))


def test_docx_member_read_is_bounded_even_if_metadata_is_wrong(helpers, monkeypatch):
    extract = helpers['make_inputs']
    monkeypatch.setattr(extract, 'DOCX_XML_MAX_BYTES', 100)
    monkeypatch.setattr(extract, 'get', lambda *args: b'zip')
    class Stream:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            assert limit == 101
            return b'x' * limit
    class Archive:
        def __init__(self, *args):
            pass
        def getinfo(self, name):
            return Namespace(file_size=1)
        def open(self, member):
            return Stream()
    monkeypatch.setattr(extract.zipfile, 'ZipFile', Archive)
    with pytest.raises(ValueError, match='expanded-size'):
        extract.excerpt('bucket', 'archive.docx', 3)


def test_vcf_wide_column_header_respects_complete_excerpt_cap(helpers, monkeypatch):
    extract = helpers['make_inputs']
    content = ('##fileformat=VCFv4.2\n#CHROM\tPOS\t' + '\t'.join('sample' + str(n) for n in range(1000)) + '\n').encode()
    monkeypatch.setattr(extract, 'get', lambda *args: content)
    extract.EXCERPT_CHARS = 100
    method, text = extract.excerpt('bucket', 'wide.vcf', len(content))
    assert len(text) == 100 and 'truncated' in method
    extract.EXCERPT_CHARS = 0
    _, full = extract.excerpt('bucket', 'wide.vcf', len(content))
    assert 'sample999' in full


def test_parent_only_quote_is_rejected_by_runner_and_verifier(helpers, tmp_path):
    row = batch_row('A', 'Direct excerpt.')
    row['parent'] = 'Evidence only in parent.'
    valid = dict(unit_id='A', what='article', summary='An article.', about=[],
                 entities=[], refers_to=[], evidence=['Evidence only in parent.'])
    class Backend:
        def call(self, rows):
            return [valid], {'cost_usd': 0}
    record, output = helpers['run_model'].call(Backend(), Namespace(model='test'), 0, ('data/', 'txt'), [row])
    assert record['error'] and not output
    inputs, outputs = tmp_path / 'inputs.csv', tmp_path / 'outputs.csv'
    with inputs.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    with outputs.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(valid))
        writer.writeheader()
        writer.writerow({key: json.dumps(value) if isinstance(value, list) else value for key, value in valid.items()})
    with pytest.raises(SystemExit) as exc:
        helpers['check_outputs'].main(inputs, outputs)
    assert exc.value.code == 1


@pytest.mark.parametrize('mode', ['cwd-default', 'explicit', 'symlink', 'hardlink'])
def test_publication_rejects_log_inside_source_before_any_push(helpers, tmp_path, monkeypatch, mode):
    export = tmp_path / 'export'
    export.mkdir()
    (export / 'document.txt').write_text('Source document')
    plan = tmp_path / 'plan.csv'
    plan.write_text('name,source\ncontext/example,export\n')
    monkeypatch.chdir(export)
    args = [str(plan), '--registry', 's3://test-lake', '--yes']
    if mode == 'explicit':
        args += ['--log', str(export / 'generated.jsonl')]
    elif mode in ('symlink', 'hardlink'):
        link = tmp_path / 'log.jsonl'
        if mode == 'symlink':
            link.symlink_to(export / 'document.txt')
        else:
            link.hardlink_to(export / 'document.txt')
        args += ['--log', str(link)]
    def forbidden(*args):
        pytest.fail('invalid log destination must fail before package construction')
    monkeypatch.setattr(helpers['publish'], 'build', forbidden)
    with pytest.raises(SystemExit) as exc:
        helpers['publish'].main(args)
    assert exc.value.code == 2
    assert sorted(p.name for p in export.iterdir()) == ['document.txt']
    assert (export / 'document.txt').read_text() == 'Source document'



@pytest.mark.parametrize('target', ['plan', 'readme'])
def test_publication_log_cannot_modify_plan_or_external_readme(helpers, tmp_path, monkeypatch, target):
    export = tmp_path / 'export'
    export.mkdir()
    (export / 'document.txt').write_text('Source document')
    readme = tmp_path / 'README.md'
    readme.write_text('Approved external README')
    plan = tmp_path / 'plan.csv'
    plan.write_text('name,source,readme\ncontext/example,export,README.md\n')
    before = (plan.read_bytes(), readme.read_bytes())
    log = plan if target == 'plan' else readme
    def forbidden(*args):
        pytest.fail('source input alias must fail before package construction')
    monkeypatch.setattr(helpers['publish'], 'build', forbidden)
    with pytest.raises(SystemExit) as exc:
        helpers['publish'].main([str(plan), '--registry', 's3://test-lake', '--yes', '--log', str(log)])
    assert exc.value.code == 2
    assert (plan.read_bytes(), readme.read_bytes()) == before
