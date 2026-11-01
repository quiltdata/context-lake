"""Synthetic, offline fidelity and CLI checks for Gmail thread conversion."""
import base64
import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def mail(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'src'))
    return importlib.import_module('context_lake.email_threads_to_markdown')


def message(mid='m1', text='Dose: 10⁶ cells at −20 °C.\n> Prior reply\n', stamp='1000'):
    return {'id': mid, 'threadId': 't1', 'internalDate': stamp,
            'payload': {'mimeType': 'text/plain',
                        'headers': [{'name': k, 'value': v} for k, v in {
                            'From': 'Analyst <a@example.test>', 'To': 'team@example.test',
                            'Cc': 'cc@example.test', 'Bcc': 'bcc@example.test',
                            'Date': 'Thu, 29 Oct 2026 10:00:00 +0000',
                            'Subject': '=?utf-8?b?UmV2aWV3IOKckw==?=',
                            'Message-ID': '<original@example.test>',
                            'In-Reply-To': '<parent@example.test>',
                            'References': '<root@example.test> <parent@example.test>'}.items()],
                        'body': {'data': base64.urlsafe_b64encode(text.encode()).decode().rstrip('=')}}}


def thread(*messages):
    return {'id': 't1', 'messages': list(messages or [message()])}


def test_metadata_exact_body_chronology_and_source_preservation(mail):
    value = thread(message('m2', 'Later message', '2000'), message())
    before = json.dumps(value)
    tid, text = mail.render_thread(value)
    assert tid == 't1' and text.startswith('# Review ✓')
    assert 'Dose: 10⁶ cells at −20 °C.\n> Prior reply\n' in text
    assert text.index('## Message m1') < text.index('## Message m2')
    for expected in ['a@example.test', 'team@example.test', 'cc@example.test', 'bcc@example.test',
                     '&lt;original@example.test&gt;', '&lt;parent@example.test&gt;',
                     'https://mail.google.com/mail/#all/t1', 'internal_date: 1000',
                     'only supplied messages']:
        assert expected in text
    assert json.dumps(value) == before


def test_connector_snake_case_and_plain_preference_with_attachment(mail):
    first = message()
    first['thread_id'] = first.pop('threadId')
    first['internal_date'] = first.pop('internalDate')
    first['payload'] = {'mime_type': 'multipart/mixed', 'parts': [
        {'mime_type': 'multipart/alternative', 'parts': [
            {'mime_type': 'text/html', 'body': {'content': '<b>duplicate HTML</b>'}},
            {'mime_type': 'text/plain', 'body': {'content': 'Exact plain body'}},
        ]},
        {'mime_type': 'application/pdf', 'filename': 'report.pdf',
         'body': {'attachment_id': 'att1', 'size': 123, 'base64_url_content': 'DO NOT DECODE'}},
        {'mime_type': 'text/plain', 'filename': 'notes.txt', 'body': {'content': 'attachment text'}},
        {'mime_type': 'text/plain', 'body': {'content': 'second inline text'}},
    ]}
    text = mail.convert({'threads': [{'thread_id': 't1', 'messages': [first]}]})['t1']
    assert 'Exact plain body' in text and 'second inline text' in text
    assert 'duplicate HTML' not in text and 'attachment text' not in text
    assert 'report.pdf' in text and 'att1' in text and '123' in text and 'notes.txt' in text
    assert 'DO NOT DECODE' not in text


def test_html_only_is_flagged_and_drops_hidden_content(mail):
    msg = message()
    msg['payload'] = {'mimeType': 'text/html', 'body': {'content':
        '<p>Hello &amp; welcome</p><script>secret()</script><style>.secret{}</style><div>Next<br>line</div>'}}
    text = mail.render_thread(thread(msg))[1]
    assert 'Body (html-derived)' in text and 'Hello & welcome' in text and 'Next\nline' in text
    assert 'secret()' not in text and '.secret' not in text


def test_charset_decode_and_date_header_fallback(mail):
    msg = message()
    msg.pop('internalDate')
    msg['payload']['headers'].append({'name': 'Content-Type', 'value': 'text/plain; charset="iso-8859-1"'})
    msg['payload']['body'] = {'data': base64.urlsafe_b64encode('café'.encode('iso-8859-1')).decode()}
    text = mail.render_thread(thread(msg))[1]
    assert 'café' in text and '- Order: chronological' in text


def test_missing_date_retains_input_order_and_marks_gap(mail):
    msg = message('m2')
    msg.pop('internalDate')
    msg['payload']['headers'] = []
    text = mail.render_thread(thread(msg, message()))[1]
    assert 'input order (one or more dates missing)' in text
    assert text.index('## Message m2') < text.index('## Message m1')


def test_body_markdown_cannot_close_generated_fence(mail):
    body = '```\n# Forged metadata\n````\n'
    text = mail.render_thread(thread(message(text=body)))[1]
    assert '`````text\n' + body + '\n`````' in text


@pytest.mark.parametrize('case', ['snippet', 'wrong-thread', 'duplicate-message', 'bad-id',
                                  'bad-base64', 'missing-text', 'attachment-text', 'bad-date',
                                  'empty-multipart', 'bad-charset'])
def test_rejects_lossy_or_malformed_input(mail, case):
    value = thread()
    msg = value['messages'][0]
    if case == 'snippet':
        msg.pop('payload')
        msg['snippet'] = 'not enough'
    elif case == 'wrong-thread':
        msg['threadId'] = 'other'
    elif case == 'duplicate-message':
        value['messages'].append(message())
    elif case == 'bad-id':
        value['id'] = '../escape'
    elif case == 'bad-base64':
        msg['payload']['body'] = {'data': '@@@'}
    elif case == 'missing-text':
        msg['payload']['body'] = {'size': 123}
    elif case == 'attachment-text':
        msg['payload']['body'] = {'attachmentId': 'separate'}
    elif case == 'bad-date':
        msg['internalDate'] = 'not a date'
    elif case == 'empty-multipart':
        msg['payload'] = {'mimeType': 'multipart/mixed', 'parts': []}
    elif case == 'bad-charset':
        msg['payload']['charset'] = 'invalid-charset'
    with pytest.raises(mail.ThreadFormatError):
        mail.convert(value)


def test_cli_validates_batch_before_writes_and_refuses_overwrite(mail, tmp_path):
    source = tmp_path / 'input.json'
    root = tmp_path / 'out'
    argv = ['--input', str(source), '--output-dir', str(root)]
    source.write_text(json.dumps([thread(), {'id': 'bad', 'messages': []}]))
    with pytest.raises(SystemExit) as exc:
        mail.main(argv)
    assert exc.value.code == 2 and not root.exists()
    original = json.dumps(thread())
    source.write_text(original)
    assert mail.main(argv) == 0
    output = root / 'threads/t1.md'
    before = output.read_text()
    with pytest.raises(SystemExit) as exc:
        mail.main(argv)
    assert exc.value.code == 2 and output.read_text() == before
    assert mail.main(argv + ['--overwrite']) == 0
    assert source.read_text() == original
    assert mail.convert([thread()])['t1'] == before
    with pytest.raises(mail.ThreadFormatError, match='duplicate thread'):
        mail.convert([thread(), thread()])


def test_cli_refuses_symlink_and_source_replacement(mail, tmp_path):
    source = tmp_path / 'input.json'
    source.write_text(json.dumps(thread()))
    root = tmp_path / 'out'
    root.mkdir()
    (root / 'threads').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(SystemExit):
        mail.main(['--input', str(source), '--output-dir', str(root)])
    (root / 'threads').unlink()
    (root / 'threads').mkdir()
    dest = root / 'threads/t1.md'
    dest.symlink_to(source)
    with pytest.raises(SystemExit):
        mail.main(['--input', str(source), '--output-dir', str(root), '--overwrite'])
    assert json.loads(source.read_text()) == thread()


@pytest.mark.parametrize('level', ['thread', 'message', 'part', 'body'])
def test_rejects_explicit_truncation(mail, level):
    value = thread()
    msg = value['messages'][0]
    target = {'thread': value, 'message': msg, 'part': msg['payload'], 'body': msg['payload']['body']}[level]
    target['content_truncated'] = True
    with pytest.raises(mail.ThreadFormatError, match='truncated'):
        mail.convert(value)


def test_cli_never_overwrites_hardlinked_source(mail, tmp_path):
    source = tmp_path / 'input.json'
    before = json.dumps(thread())
    source.write_text(before)
    output = tmp_path / 'out/threads'
    output.mkdir(parents=True)
    (output / 't1.md').hardlink_to(source)
    with pytest.raises(SystemExit):
        mail.main(['--input', str(source), '--output-dir', str(output.parent), '--overwrite'])
    assert source.read_text() == before


def test_empty_plain_alternative_keeps_nonempty_html(mail):
    msg = message()
    msg['payload'] = {'mimeType': 'multipart/alternative', 'parts': [
        {'mimeType': 'text/plain', 'body': {'content': '  \n'}},
        {'mimeType': 'text/html', 'body': {'content': '<p>Substantive reply</p>'}},
    ]}
    text = mail.render_thread(thread(msg))[1]
    assert 'Body (html-derived)' in text and 'Substantive reply' in text


def test_header_markdown_stays_literal(mail):
    msg = message()
    msg['payload']['headers'] = [{'name': 'Subject', 'value': '![tracking](https://example.test/pixel.png) <img src=x>'}]
    text = mail.render_thread(thread(msg))[1]
    assert '![tracking](' not in text and '<img' not in text
    assert r'\!\[tracking\]\(https://example.test/pixel.png\)' in text
    assert '&lt;img src=x&gt;' in text


@pytest.mark.parametrize('mime', [None, '', '   '])
def test_metadata_only_payload_is_rejected(mail, mime):
    msg = message()
    msg['snippet'] = 'Preview only'
    msg['payload'] = {'headers': msg['payload']['headers']}
    if mime is not None:
        msg['payload']['mime_type'] = mime
    with pytest.raises(mail.ThreadFormatError, match='MIME type'):
        mail.convert(thread(msg))


def test_truncated_wrapper_is_rejected_before_unwrapping(mail):
    with pytest.raises(mail.ThreadFormatError, match='truncated'):
        mail.convert({'threads': [thread()], 'content_truncated': True})
