"""Offline integration checks for indexing and portable backfill."""
import importlib
import json
from argparse import Namespace
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'src/context_lake'


@pytest.fixture
def tools(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS.parent))
    return Namespace(parser=importlib.import_module('context_lake.fathom_to_markdown'),
                     index=importlib.import_module('context_lake.transcript_index'),
                     backfill=importlib.import_module('context_lake.backfill_transcripts'))


def transcript(tools, root, recording_id=123, title='Example meeting', day='2026-10-28', text='Protein abundance measured.'):
    relative = tools.parser.package_path(title, day, recording_id, 'https://fathom.video/calls/456')
    rendered = tools.parser.render_markdown(title, day, recording_id,
        'https://fathom.video/calls/456', '[00:01](https://fathom.video/calls/456?timestamp=1) Analyst: ' + text)
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered)
    return path, rendered


def test_index_handles_json_quoted_titles_and_is_deterministic(tools, tmp_path, capsys):
    title = 'Review "signals" \\ workflow'
    path, rendered = transcript(tools, tmp_path, title=title,
                                text='Protein abundance measured. Protein abundance measured.')
    transcript(tools, tmp_path, recording_id=789, title='Imaging', day='2026-10-29',
               text='Microscope images acquired. Microscope images acquired.')
    args = ['--package-dir', str(tmp_path), '--min-count', '1']
    assert tools.index.main(args) == 0
    first = capsys.readouterr().out
    assert tools.index.main(args) == 0
    assert capsys.readouterr().out == first
    assert title in first and str(path.relative_to(tmp_path)) in first
    assert '2 transcripts' in first and '## Themes' in first
    assert 'protein abundance' in first and 'microscope images' in first
    assert 'FATHOM_API_KEY' not in first
    assert path.read_text() == rendered
    output_args = args + ['--output-dir', str(tmp_path)]
    assert tools.index.main(output_args) == 0
    assert (tmp_path / 'INDEX.md').read_text() == first
    with pytest.raises(SystemExit) as exc:
        tools.index.main(output_args)
    assert exc.value.code == 2
    assert tools.index.main(output_args + ['--overwrite']) == 0
    assert (tmp_path / 'INDEX.md').read_text() == first


def test_index_rejects_invalid_json_quoted_title_before_writing(tools, tmp_path):
    path, _ = transcript(tools, tmp_path)
    path.write_text(path.read_text().replace('title: "Example meeting"', 'title: ' + json.dumps('bad\nname')))
    with pytest.raises(SystemExit) as exc:
        tools.index.main(['--package-dir', str(tmp_path), '--output-dir', str(tmp_path)])
    assert exc.value.code == 2 and not (tmp_path / 'INDEX.md').exists()


def test_backfill_dry_run_is_scoped_and_needs_no_credentials(tools, tmp_path, monkeypatch, capsys):
    path, _ = transcript(tools, tmp_path)
    path.write_text('')
    other, _ = transcript(tools, tmp_path, recording_id=789, day='2026-10-29')
    monkeypatch.delenv('FATHOM_API_KEY', raising=False)
    monkeypatch.setattr(tools.backfill, 'resolve_api_key', lambda: pytest.fail('dry run read credentials'))
    monkeypatch.setattr(tools.backfill, 'import_recording', lambda *a, **kw: pytest.fail('dry run fetched data'))
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert tools.backfill.main(['--package-dir', str(tmp_path), '--dry-run']) == 0
    out = capsys.readouterr().out
    assert path.name in out and other.name not in out
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_backfill_repairs_and_indexes_only_selected_placeholders(tools, tmp_path, monkeypatch):
    root = tmp_path / 'export'
    path, rendered = transcript(tools, root)
    path.write_text('')
    keep, kept = transcript(tools, root, recording_id=789)
    requests = []
    def fetch(recording_id, api_key, **kwargs):
        requests.append(recording_id)
        return tools.parser.ImportResult(str(path.relative_to(root)), rendered, b'{"transcript": []}')
    monkeypatch.setattr(tools.backfill, 'resolve_api_key', lambda: 'synthetic-key')
    monkeypatch.setattr(tools.backfill, 'import_recording', fetch)
    raw = tmp_path / 'raw'
    assert tools.backfill.main(['--package-dir', str(root), '--save-json', str(raw), '--sleep', '0']) == 0
    assert requests == [123] and path.read_text() == rendered and keep.read_text() == kept
    assert (raw / '123.json').read_bytes() == b'{"transcript": []}'
    assert tools.index.main(['--package-dir', str(root), '--output-dir', str(root)]) == 0
    assert '2 transcripts' in (root / 'INDEX.md').read_text()


def test_backfill_rejects_renamed_meeting_before_writing(tools, tmp_path, monkeypatch):
    path, _ = transcript(tools, tmp_path)
    path.write_text('')
    monkeypatch.setattr(tools.backfill, 'resolve_api_key', lambda: 'synthetic-key')
    monkeypatch.setattr(tools.backfill, 'import_recording', lambda *a, **kw:
                        tools.parser.ImportResult('transcripts/2026-10-28--renamed--123.md', 'new', b'{}'))
    assert tools.backfill.main(['--package-dir', str(tmp_path), '--sleep', '0']) == 1
    assert path.read_bytes() == b''
    assert list((tmp_path / 'transcripts').iterdir()) == [path]


def test_backfill_continues_after_failure_and_paces_calls(tools, tmp_path, monkeypatch):
    first, _ = transcript(tools, tmp_path)
    first.write_text('')
    second, rendered = transcript(tools, tmp_path, recording_id=789, day='2026-10-29')
    second.write_text('')
    delays = []
    monkeypatch.setattr(tools.backfill.time, 'sleep', delays.append)
    monkeypatch.setattr(tools.backfill, 'resolve_api_key', lambda: 'synthetic-key')
    def fetch(recording_id, *a, **kw):
        if recording_id == 123:
            raise tools.parser.FathomAPIError('mock unavailable')
        return tools.parser.ImportResult(str(second.relative_to(tmp_path)), rendered, b'{}')
    monkeypatch.setattr(tools.backfill, 'import_recording', fetch)
    assert tools.backfill.main(['--package-dir', str(tmp_path), '--sleep', '0.25']) == 1
    assert delays == [0.25] and first.read_bytes() == b'' and second.read_text() == rendered


def test_backfill_force_is_explicit_and_limit_is_respected(tools, tmp_path, monkeypatch):
    first, rendered = transcript(tools, tmp_path)
    second, unchanged = transcript(tools, tmp_path, recording_id=789, day='2026-10-29')
    requests = []
    monkeypatch.setattr(tools.backfill, 'resolve_api_key', lambda: 'synthetic-key')
    def fetch(recording_id, *a, **kw):
        requests.append(recording_id)
        return tools.parser.ImportResult(str(first.relative_to(tmp_path)), rendered, b'{}')
    monkeypatch.setattr(tools.backfill, 'import_recording', fetch)
    assert tools.backfill.main(['--package-dir', str(tmp_path)]) == 0
    assert requests == []
    assert tools.backfill.main(['--package-dir', str(tmp_path), '--force', '--limit', '1']) == 0
    assert requests == [123] and second.read_text() == unchanged


@pytest.mark.parametrize('flag,value', [('--sleep', 'nan'), ('--max-pages', '0'), ('--limit', '0')])
def test_backfill_invalid_arguments_fail_before_fetch(tools, tmp_path, monkeypatch, flag, value):
    monkeypatch.setattr(tools.backfill, 'import_recording', lambda *a, **kw: pytest.fail('unexpected fetch'))
    with pytest.raises(SystemExit) as exc:
        tools.backfill.main(['--package-dir', str(tmp_path), flag, value])
    assert exc.value.code == 2


@pytest.mark.parametrize('separate', [False, True])
def test_index_links_resolve_relative_to_output_directory(tools, tmp_path, separate):
    import re
    from urllib.parse import unquote
    root = tmp_path / 'export with spaces'
    path, _ = transcript(tools, root)
    output = tmp_path / 'reports' if separate else root / 'reports'
    assert tools.index.main(['--package-dir', str(root), '--output-dir', str(output)]) == 0
    index = (output / 'INDEX.md').read_text()
    href = re.search(r'\[Transcript\]\(([^)]+)\)', index).group(1)
    assert ' ' not in href
    assert (output / unquote(href)).resolve() == path.resolve()
    assert (output / unquote(href)).is_file()


@pytest.mark.parametrize('alias', ['same-path', 'hardlink'])
def test_offline_import_cannot_replace_input_even_with_overwrite(tools, tmp_path, alias):
    relative = tools.parser.package_path('Example meeting', '2026-10-28', 123, 'https://fathom.video/calls/456')
    dest = tmp_path / relative
    dest.parent.mkdir(parents=True)
    raw = '[00:01](https://fathom.video/calls/456?timestamp=1) Analyst: Exact words.\n'
    dest.write_text(raw)
    source = dest
    if alias == 'hardlink':
        source = tmp_path / 'source.txt'
        source.hardlink_to(dest)
    with pytest.raises(SystemExit) as exc:
        tools.parser.main(['--input', str(source), '--recording-id', '123', '--title', 'Example meeting',
                           '--date', '2026-10-28', '--source-url', 'https://fathom.video/calls/456',
                           '--output-dir', str(tmp_path), '--overwrite'])
    assert exc.value.code == 2 and source.read_text() == raw and dest.read_text() == raw


def test_importer_and_indexer_accept_colons_in_speaker_labels(tools, tmp_path, capsys):
    relative = tools.parser.package_path('Example meeting', '2026-10-28', 123, 'https://fathom.video/calls/456')
    body = '[00:01](https://fathom.video/calls/456?timestamp=1) Analyst:A: Exact words.\n'
    rendered = tools.parser.render_markdown('Example meeting', '2026-10-28', 123,
                                          'https://fathom.video/calls/456', body)
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(rendered)
    assert tools.index.main(['--package-dir', str(tmp_path)]) == 0
    assert 'Example meeting' in capsys.readouterr().out



def test_api_segment_bare_colon_speaker_renders_and_indexes(tools, tmp_path, capsys):
    body = tools.parser.transcript_from_api_segments([
        {'timestamp': '00:00:01', 'speaker': {'display_name': 'Analyst:A'}, 'text': 'Exact words.'}
    ], 'https://fathom.video/calls/456')
    assert 'Analyst:A: Exact words.' in body
    relative = tools.parser.package_path('Example meeting', '2026-10-28', 123, 'https://fathom.video/calls/456')
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(tools.parser.render_markdown('Example meeting', '2026-10-28', 123,
                                               'https://fathom.video/calls/456', body))
    assert tools.index.main(['--package-dir', str(tmp_path)]) == 0
    assert 'Example meeting' in capsys.readouterr().out
    with pytest.raises(tools.parser.TranscriptFormatError, match='delimiter'):
        tools.parser.transcript_from_api_segments([
            {'timestamp': '00:00:01', 'speaker': {'display_name': 'Analyst: A'}, 'text': 'Exact words.'}
        ], 'https://fathom.video/calls/456')


EXPORT = (
    'Example meeting - October 28\n'
    'VIEW RECORDING - 62 mins (3 mins of highlights): https://fathom.video/share/abc\n'
    '\n'
    '---\n'
    '\n'
    '0:00 - Analyst (example.org)\n'
    '  First utterance.  Two spaces kept.\n'
    '  Second line of the same turn.\n'
    '\n'
    '1:05 - Reviewer\n'
    '  Reply: with a colon.\n'
    '\n'
    '1:01:02 - Analyst (example.org)\n'
    '  Past the hour.\n'
)
CALL = 'https://fathom.video/calls/456'


def test_export_matches_canonical_lines_verbatim(tools):
    body = tools.parser.transcript_from_export(EXPORT, CALL)
    assert body.splitlines() == [
        f'[00:00]({CALL}?timestamp=0) Analyst (example.org): First utterance.  Two spaces kept. '
        'Second line of the same turn.',
        f'[01:05]({CALL}?timestamp=65) Reviewer: Reply: with a colon.',
        f'[1:01:02]({CALL}?timestamp=3662) Analyst (example.org): Past the hour.',
    ]
    assert tools.parser.normalize_transcript(body, CALL) == body


def test_export_no_coalesce_keeps_one_line_per_text_line(tools):
    body = tools.parser.transcript_from_export(EXPORT, CALL, coalesce=False)
    assert len(body.splitlines()) == 4
    assert body.splitlines()[1] == f'[00:00]({CALL}?timestamp=0) Analyst (example.org): Second line of the same turn.'


PASTED_VARIANTS = {
    'no header, no indent, CRLF, BOM': '\ufeff' + (
        '0:00 - Analyst (example.org)\r\nFirst utterance.  Two spaces kept.\r\n'
        'Second line of the same turn.\r\n1:05 - Reviewer\r\nReply: with a colon.\r\n'
        '1:01:02 - Analyst (example.org)\r\nPast the hour.\r\n'),
    'tabs, trailing spaces, en dash, nbsp': (
        'Example meeting\n\n0:00 \u2013 Analyst (example.org)  \n\tFirst utterance.  Two spaces kept.\n'
        '\tSecond line of the same turn.\n\n1:05\u00a0-\u00a0Reviewer\n    Reply: with a colon.\n\n'
        '1:01:02 \u2014 Analyst (example.org)\n\tPast the hour.\n'),
}


@pytest.mark.parametrize('variant', sorted(PASTED_VARIANTS))
def test_pasted_variants_render_like_the_native_download(tools, variant):
    expected = tools.parser.transcript_from_export(EXPORT, CALL)
    assert tools.parser.transcript_from_export(PASTED_VARIANTS[variant], CALL) == expected


def test_indented_timestamp_like_text_is_never_a_heading(tools):
    body = tools.parser.transcript_from_export('0:00 - Analyst\n  5:30 - the time we started.\n', CALL)
    assert body == f'[00:00]({CALL}?timestamp=0) Analyst: 5:30 - the time we started.'


def test_nesting_deeper_than_the_text_indent_is_kept(tools):
    native = '0:00 - Analyst\n  HIGHLIGHT: Key point\n    End of rant.\n'
    pasted = '0:00 - Analyst\n\tHIGHLIGHT: Key point\n\t  End of rant.\n'
    unindented = '0:00 - Analyst\nHIGHLIGHT: Key point\n  End of rant.\n'
    assert tools.parser.transcript_from_export(unindented, CALL) == (
        f'[00:00]({CALL}?timestamp=0) Analyst: HIGHLIGHT: Key point   End of rant.')
    expected = f'[00:00]({CALL}?timestamp=0) Analyst: HIGHLIGHT: Key point   End of rant.'
    assert tools.parser.transcript_from_export(native, CALL) == expected
    assert tools.parser.transcript_from_export(pasted, CALL) == expected


def test_trailing_whitespace_in_spoken_text_is_kept(tools):
    body = tools.parser.transcript_from_export('0:00 - Analyst\n  End of rant.  \n  Questions?\n', CALL)
    assert body == f'[00:00]({CALL}?timestamp=0) Analyst: End of rant.   Questions?'


def test_heading_without_text_is_skipped_like_a_blank_api_segment(tools):
    body = tools.parser.transcript_from_export('0:00 - Analyst\n\n0:05 - Reviewer\n  Words.\n', CALL)
    assert body == f'[00:05]({CALL}?timestamp=5) Reviewer: Words.'


@pytest.mark.parametrize('export, message', [
    ('  indented before any heading\n', 'no .* turns with text'),
    ('title only\n---\n\n', 'no .* turns with text'),
    ('0:10 - Analyst\n  Late.\n  More.\n\n0:05 - Reviewer\n  Early.\n',
     'export line 5: timestamp 0:05 precedes the previous turn'),
    ('header\n---\n0:00 - Doe: Jane\n  Words.\n', "export line 3: speaker name 'Doe: Jane' contains ': '"),
    ('0:00 - Analyst\n  Words.\n0:75 - Reviewer\n  Words.\n', 'export line 3: invalid API timestamp'),
])
def test_export_rejects_malformed_input(tools, export, message):
    with pytest.raises(tools.parser.TranscriptFormatError, match=message):
        tools.parser.transcript_from_export(export, CALL)


@pytest.mark.parametrize('raw, expected', [
    (EXPORT, 'export'),
    (PASTED_VARIANTS['no header, no indent, CRLF, BOM'], 'export'),
    (f'\n[00:01]({CALL}?timestamp=1) Analyst: Exact words.\n', 'lines'),
])
def test_detect_input_format(tools, raw, expected):
    assert tools.parser.detect_input_format(raw) == expected


def test_cli_renders_export_and_indexes(tools, tmp_path, capsys):
    source = tmp_path / 'export.txt'
    source.write_text(EXPORT)
    out = tmp_path / 'pkg'
    assert tools.parser.main(['--input', str(source), '--recording-id', '123',
                              '--title', 'Example meeting', '--date', '2026-10-28', '--source-url', CALL,
                              '--output-dir', str(out)]) == 0
    rendered = (out / tools.parser.package_path('Example meeting', '2026-10-28', 123, CALL)).read_text()
    assert 'Analyst (example.org): First utterance.  Two spaces kept. Second line' in rendered
    assert tools.index.main(['--package-dir', str(out)]) == 0
    assert '3 segments' in capsys.readouterr().out


@pytest.mark.parametrize('input_format', ['auto', 'export', 'lines'])
def test_any_explicit_input_format_requires_input(tools, monkeypatch, input_format):
    monkeypatch.setattr(tools.parser, 'resolve_api_key', lambda: pytest.fail('should fail before fetching'))
    with pytest.raises(SystemExit) as exc:
        tools.parser.main(['--recording-id', '123', '--input-format', input_format])
    assert exc.value.code == 2
