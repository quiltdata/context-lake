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
    tid, text = mail.render_thread(value, strip_quotes=False)
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


LIST = 'list@groups.example.test'
FOOTER = ('\n--\nInvite friends: http://example.test/join\n---\n'
          'You received this message because you are subscribed to the Google Groups "list" group.\n'
          'To unsubscribe from this group and stop receiving emails from it, send an email to x@example.test.\n'
          'To view this discussion visit https://groups.google.com/d/msgid/list/ABC_1%40mail.example.test.')


def flat(mid, body=None, to=(LIST,), cc=(), stamp='1000', **extra):
    """A Gmail connector message: flattened fields, no MIME payload."""
    value = {'id': mid, 'threadId': 't1', 'sender': 'a@example.test', 'toRecipients': list(to),
             'ccRecipients': list(cc), 'date': '2026-08-31T05:35:27Z', 'internalDate': stamp,
             'subject': 'Re: [list] Boundaries', **extra}
    if body is not None:
        value['plaintextBody'] = body
    return value


def test_connector_flat_message_renders_headers_body_and_attachments(mail):
    value = {'id': 't1', 'messages': [flat('m1', 'Exact  words.\n', attachments=[
        {'filename': 'shot.png', 'mimeType': 'image/png', 'partId': '1.1', 'id': 'att1'}])]}
    _, text = mail.render_thread(value, strip_quotes=False)
    assert f'- to: {LIST}' in text and '- from: a@example.test' in text
    assert '```text\nExact  words.\n\n```' in text
    assert '"filename": "shot.png"' in text and '"part\\_id": "1.1"' in text


def test_flat_empty_plaintext_falls_back_to_html(mail):
    msg = flat('m1')
    msg['plaintextBody'] = '  \n'
    msg['htmlBody'] = '<p>Substantive</p>'
    _, text = mail.render_thread({'id': 't1', 'messages': [msg]}, strip_quotes=False)
    assert 'Body (html-derived)' in text and 'Substantive' in text


def test_list_filter_drops_off_list_replies_and_reports_counts(mail):
    value = {'id': 't1', 'messageCount': 4, 'messages': [
        flat('m1', 'To the list.'), flat('m2', to=('a@example.test',), stamp='2000'),
        flat('m3', 'Cc the list.', to=('b@example.test',), cc=(LIST.upper(),), stamp='3000')]}
    _, text = mail.render_thread(value, list_address=LIST, strip_quotes=False)
    assert '## Message m1' in text and '## Message m3' in text and 'm2' not in text
    assert '(2 of 3 supplied kept; off-list replies omitted)' in text
    assert '- Gmail message count: 4 (3 supplied' in text


def test_list_filter_uses_exact_mailbox_not_substring(mail):
    similar = 'notlist@groups.example.test'
    display = '"list@groups.example.test" <person@example.test>'
    value = {'id': 't1', 'messages': [
        flat('m1', 'On list.'),
        flat('m2', 'Similar domain.', to=(similar,), stamp='2000'),
        flat('m3', 'Display name only.', to=(display,), stamp='3000'),
    ]}
    _, text = mail.render_thread(value, list_address=LIST, strip_quotes=False)
    assert '## Message m1' in text and 'm2' not in text and 'm3' not in text
    assert '(1 of 3 supplied kept' in text


def test_list_filter_skips_threads_without_list_messages(mail, tmp_path):
    source = tmp_path / 'threads.json'
    source.write_text(json.dumps({'threads': [
        {'id': 't1', 'messages': [flat('m1', to=('a@example.test',))]},
        {'id': 't2', 'messages': [{**flat('m2', 'Kept.'), 'threadId': 't2'}]}]}))
    assert mail.main(['--input', str(source), '--output-dir', str(tmp_path / 'out'),
                      '--list', LIST, '--keep-quotes']) == 0
    assert [p.name for p in (tmp_path / 'out' / 'threads').iterdir()] == ['t2.md']


def test_list_overwrite_removes_stale_thread_files(mail, tmp_path):
    source = tmp_path / 'threads.json'
    out = tmp_path / 'out'
    threads = out / 'threads'
    threads.mkdir(parents=True)
    (threads / 'old.md').write_text('private off-list mail')
    (threads / 't2.md').write_text('previous')
    source.write_text(json.dumps({'threads': [
        {'id': 't2', 'messages': [{**flat('m2', 'Kept.'), 'threadId': 't2'}]}]}))
    with pytest.raises(SystemExit):
        mail.main(['--input', str(source), '--output-dir', str(out), '--list', LIST, '--keep-quotes'])
    assert (threads / 'old.md').read_text() == 'private off-list mail'
    assert mail.main(['--input', str(source), '--output-dir', str(out),
                      '--list', LIST, '--keep-quotes', '--overwrite']) == 0
    assert [p.name for p in threads.iterdir()] == ['t2.md']
    assert 'Kept.' in (threads / 't2.md').read_text()


def test_kept_flat_message_without_body_is_rejected(mail):
    with pytest.raises(mail.ThreadFormatError, match='m1 has no text body'):
        mail.render_thread({'id': 't1', 'messages': [flat('m1')]}, list_address=LIST)


def test_iso8601_connector_date_orders_without_internal_date(mail):
    msg = flat('m1', 'Body.')
    msg.pop('internalDate')
    _, text = mail.render_thread({'id': 't1', 'messages': [msg]}, strip_quotes=False)
    assert '- Order: chronological' in text
    assert '2026-08-31T05:35:27Z' in text


@pytest.mark.parametrize('body, expected', [
    ('New text.\n\nOn Mon, Aug 31, 2026 at 7:57 AM Ben <ben@example.test> wrote:\n\n> old\n>\n> older\n',
     'New text.'),
    ("New text.\n\nOn Sun, Aug 30, 2026 at 10:40 PM 'Ernest' via list <\nlist@groups.example.test> wrote:\n\n> old\n"
     + FOOTER, 'New text.'),
    ('Thanks!\n\n> On Aug 30, 2026, at 10:40 PM, Adam <adam@example.test> wrote:\n>\n> old\n', 'Thanks!'),
    ('+1\n\n+1 555 0100\n--\n\nOn Mon, Aug 31, 2026 at 7:57 AM Ben wrote:\n\n> old\n', '+1\n\n+1 555 0100'),
    ('Hi,\n\n> quoted question\n\nMy answer.\n\n> second question\n\nSecond answer.\n',
     'Hi,\n\n> quoted question\n\nMy answer.\n\n> second question\n\nSecond answer.\n'),
    ('> entirely quoted\n> forward\n', '> entirely quoted\n> forward\n'),
])
def test_strip_quoted_keeps_new_and_inline_text(mail, body, expected):
    assert mail.strip_quoted(body)[0] == expected


def test_strip_quotes_records_groups_link_unescaped(mail):
    value = {'id': 't1', 'messages': [flat('m1', 'Reply.\n' + FOOTER)]}
    _, text = mail.render_thread(value, list_address=LIST, strip_quotes=True)
    assert '- groups-link: <https://groups.google.com/d/msgid/list/ABC_1%40mail.example.test>' in text
    assert 'You received this message' not in text and '```text\nReply.\n```' in text
    assert 'quoted reply history and Google Groups footers stripped' in text


def test_html_derived_footer_does_not_drop_bottom_reply(mail):
    # VisibleHTML flattens <blockquote>; footer rule must not erase a trailing reply.
    html = ('<blockquote><p>You received this message because you are subscribed to the Google Groups '
            '"list" group.</p><p>To view this discussion visit '
            'https://groups.google.com/d/msgid/list/ABC_1%40mail.example.test.</p></blockquote>'
            '<p>My bottom reply</p>')
    msg = flat('m1')
    msg.pop('plaintextBody', None)
    msg['htmlBody'] = html
    _, text = mail.render_thread({'id': 't1', 'messages': [msg]}, strip_quotes=True)
    assert 'My bottom reply' in text


def test_quotes_and_footers_kept_when_stripping_is_off(mail):
    _, text = mail.render_thread({'id': 't1', 'messages': [flat('m1', 'Reply.\n' + FOOTER)]}, strip_quotes=False)
    assert 'You received this message' in text and 'quoted history retained' in text


def test_stripping_leaves_unquoted_text_byte_identical(mail):
    for body in ('Exact  words.\n', 'Trailing blanks\n\n\n', 'No newline', '-- \nSignature only\n'):
        assert mail.strip_quoted(body) == (body, None)


def test_cli_strips_quotes_by_default_and_keep_quotes_opts_out(mail, tmp_path):
    source = tmp_path / 'threads.json'
    source.write_text(json.dumps({'id': 't1', 'messages': [flat('m1', 'Reply.\n\nOn Mon Ben wrote:\n\n> old\n')]}))
    assert mail.main(['--input', str(source), '--output-dir', str(tmp_path / 'a')]) == 0
    assert '> old' not in (tmp_path / 'a' / 'threads' / 't1.md').read_text()
    assert mail.main(['--input', str(source), '--output-dir', str(tmp_path / 'b'), '--keep-quotes']) == 0
    assert '> old' in (tmp_path / 'b' / 'threads' / 't1.md').read_text()


def test_cli_warns_when_group_mail_has_no_list_filter(mail, tmp_path, capsys):
    source = tmp_path / 'threads.json'
    source.write_text(json.dumps({'id': 't1', 'messages': [flat('m1', 'Reply.\n' + FOOTER)]}))
    assert mail.main(['--input', str(source), '--output-dir', str(tmp_path / 'a'), '--keep-quotes']) == 0
    assert 'warning: messages carry Google Groups footers ("list")' in capsys.readouterr().err
    assert mail.main(['--input', str(source), '--output-dir', str(tmp_path / 'b'),
                      '--list', LIST, '--keep-quotes']) == 0
    assert 'warning' not in capsys.readouterr().err


def test_quoted_group_footer_alone_does_not_warn(mail, tmp_path, capsys):
    source = tmp_path / 'threads.json'
    quoted = '\n'.join('> ' + line for line in FOOTER.splitlines())
    source.write_text(json.dumps({'id': 't1', 'messages': [flat('m1', 'Reply.\n\nOn Mon Ben wrote:\n' + quoted)]}))
    assert mail.main(['--input', str(source), '--output-dir', str(tmp_path / 'a'), '--keep-quotes']) == 0
    assert 'warning' not in capsys.readouterr().err


def test_missing_body_error_suggests_list_for_group_mail(mail, tmp_path, capsys):
    source = tmp_path / 'threads.json'
    source.write_text(json.dumps({'id': 't1', 'messages': [
        flat('m1', 'Reply.\n' + FOOTER), flat('m2', to=('a@example.test',), stamp='2000')]}))
    with pytest.raises(SystemExit):
        mail.main(['--input', str(source), '--output-dir', str(tmp_path / 'a'), '--keep-quotes'])
    err = capsys.readouterr().err
    assert 'm2 has no text body' in err and 'Pass --list ADDRESS' in err


def test_footer_inside_body_is_content_not_stripped(mail):
    digest = ('Topic: One\n\nFirst post.\n' + FOOTER + '\n\nTopic: Two\n\nSecond post.\n\n--\n'
              'You received this digest because you are subscribed to updates for this group.\n')
    assert mail.strip_quoted(digest) == (digest, None)
    text, link = mail.strip_quoted('Reply.\n' + FOOTER + '\n')
    assert text == 'Reply.' and link.endswith('ABC_1%40mail.example.test')
