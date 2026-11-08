#!/usr/bin/env python3
"""Convert saved Gmail API/connector thread JSON to Markdown; no network calls."""
from __future__ import annotations

import argparse
import base64
import binascii
import html
import json
import re
import sys
from datetime import timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path


class ThreadFormatError(ValueError):
    """The export cannot be rendered without silently losing information."""


class VisibleHTML(HTMLParser):
    """Best-effort visible text; omit scripts/styles and preserve block boundaries."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if not self.hidden and tag in ('br', 'p', 'div', 'li', 'tr', 'blockquote', 'pre'):
            self.text.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self.hidden:
            self.hidden -= 1
        elif not self.hidden and tag in ('p', 'div', 'li', 'tr', 'blockquote', 'pre'):
            self.text.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def field(obj, camel, snake, default=None):
    return obj.get(camel, obj.get(snake, default))


def identifier(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise ThreadFormatError(f'{label} must be a nonempty Gmail ID (letters, digits, _ or -)')
    return value


def headers(payload):
    values = payload.get('headers', [])
    if isinstance(values, dict):
        values = [{'name': name, 'value': value} for name, value in values.items()]
    if not isinstance(values, list):
        raise ThreadFormatError('headers must be a list or object')
    result = {}
    for entry in values:
        if not isinstance(entry, dict) or not isinstance(entry.get('name'), str) or not isinstance(entry.get('value'), str):
            raise ThreadFormatError('each header needs string name and value')
        name = entry['name'].lower()
        try:
            value = str(make_header(decode_header(entry['value'])))
        except (LookupError, UnicodeError) as error:
            raise ThreadFormatError(f'invalid encoded header {name}') from error
        result[name] = result.get(name, '') + ('; ' if name in result else '') + value
    return result


def require_complete(obj):
    if any(obj.get(key) is True for key in ('content_truncated', 'truncated', 'is_truncated')):
        raise ThreadFormatError('input is marked truncated; fetch complete content before conversion')


def body_text(part):
    body = part.get('body', {})
    if not isinstance(body, dict):
        raise ThreadFormatError('MIME body must be an object')
    require_complete(body)
    if 'content' in body:
        if not isinstance(body['content'], str):
            raise ThreadFormatError('text body content must be a string')
        return body['content']
    encoded = field(body, 'data', 'base64_url_content')
    if encoded is None:
        if field(body, 'attachmentId', 'attachment_id'):
            raise ThreadFormatError('text body is stored separately; fetch it before conversion')
        if body.get('size', 0):
            raise ThreadFormatError('nonempty text body is missing content')
        return ''
    if not isinstance(encoded, str):
        raise ThreadFormatError('base64url body must be a string')
    charset = part.get('charset', 'utf-8')
    for header, value in headers(part).items():
        if header == 'content-type':
            match = re.search(r'charset\s*=\s*["\']?([^;"\'\s]+)', value, re.I)
            if match:
                charset = match[1]
    try:
        raw = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
        return raw.decode(charset)
    except (binascii.Error, ValueError, LookupError, UnicodeError) as error:
        raise ThreadFormatError('invalid base64url text or unsupported charset') from error


def mime_content(part):
    """Return text segments and attachment metadata without reading attachments."""
    if not isinstance(part, dict):
        raise ThreadFormatError('MIME part must be an object')
    require_complete(part)
    kind = field(part, 'mimeType', 'mime_type')
    if not isinstance(kind, str) or not kind.strip():
        raise ThreadFormatError('full MIME content requires a nonempty MIME type')
    kind = kind.lower()
    filename = part.get('filename', '')
    disposition = part.get('content_disposition', headers(part).get('content-disposition', ''))
    if filename or disposition.lower().startswith('attachment'):
        body = part.get('body') or {}
        return [], [{'filename': filename or '(unnamed)', 'mime_type': kind,
                     'attachment_id': field(body, 'attachmentId', 'attachment_id'),
                     'size': body.get('size')}]
    if kind.startswith('multipart/'):
        parts = part.get('parts', [])
        if not isinstance(parts, list) or not parts:
            raise ThreadFormatError('multipart message has no MIME parts')
        rendered = [mime_content(child) for child in parts]
        attachments = [a for _, items in rendered for a in items]
        if kind == 'multipart/alternative':
            # Prefer one representation; do not duplicate the same body in HTML.
            choice = next((texts for texts, _ in rendered if any(fmt == 'plain' and text.strip() for fmt, text in texts)),
                          next((texts for texts, _ in rendered if any(text.strip() for _, text in texts)),
                               next((texts for texts, _ in rendered if texts), [])))
            return choice, attachments
        return [text for texts, _ in rendered for text in texts], attachments
    if kind == 'text/plain':
        return [('plain', body_text(part))], []
    if kind == 'text/html':
        parser = VisibleHTML()
        parser.feed(body_text(part))
        return [('html-derived', ''.join(parser.text).strip())], []
    # Unknown/binary parts are recorded, never silently mistaken for message text.
    return [], [{'filename': '(unnamed MIME part)', 'mime_type': kind or '(unknown)',
                 'attachment_id': field(part.get('body') or {}, 'attachmentId', 'attachment_id')}]


def safe(value):
    text = html.escape(str(value), quote=False).replace('\r', ' ').replace('\n', ' ')
    return re.sub(r"([\\`*_\[\]{}()#!|])", r"\\\1", text)


def fence(text):
    delimiter = '`' * max(3, max((len(m[0]) + 1 for m in re.finditer(r'`+', text)), default=0))
    return f'{delimiter}text\n{text}\n{delimiter}'


def message_time(message, values):
    stamp = field(message, 'internalDate', 'internal_date')
    if stamp is not None:
        try:
            return int(stamp) / 1000
        except (TypeError, ValueError) as error:
            raise ThreadFormatError('internal_date must be epoch milliseconds') from error
    if values.get('date'):
        try:
            date = parsedate_to_datetime(values['date'])
            return date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp()
        except (TypeError, ValueError, OverflowError) as error:
            raise ThreadFormatError('invalid Date header; supply internal_date for ordering') from error
    return None


def render_thread(thread):
    if not isinstance(thread, dict):
        raise ThreadFormatError('each thread must be an object')
    require_complete(thread)
    tid = identifier(field(thread, 'id', 'thread_id'), 'thread id')
    messages = thread.get('messages')
    if not isinstance(messages, list) or not messages:
        raise ThreadFormatError(f'thread {tid} has no messages; snippets are insufficient')
    seen = set()
    records = []
    for message in messages:
        if not isinstance(message, dict):
            raise ThreadFormatError('message must be an object')
        require_complete(message)
        mid = identifier(message.get('id'), 'message id')
        if mid in seen:
            raise ThreadFormatError(f'duplicate message id: {mid}')
        seen.add(mid)
        if field(message, 'threadId', 'thread_id', tid) != tid:
            raise ThreadFormatError(f'message {mid} belongs to another thread')
        payload = message.get('payload')
        if not isinstance(payload, dict):
            raise ThreadFormatError(f'message {mid} needs a full MIME payload')
        values = headers(payload)
        texts, attachments = mime_content(payload)
        records.append((message_time(message, values), mid, message, values, texts, attachments))
    dated = all(record[0] is not None for record in records)
    if dated:
        records.sort(key=lambda record: (record[0], record[1]))
    title = records[0][3].get('subject', '(no subject)')
    lines = [f'# {safe(title)}', '', f'- Thread ID: {tid}',
             f'- Source: https://mail.google.com/mail/#all/{tid}',
             f'- Exported messages: {len(records)}',
             '- Completeness: only supplied messages; connector limits may omit older messages.',
             '- Fidelity: quoted history retained; attachments listed but not fetched; HTML-only bodies use derived visible text.',
             '- Order: ' + ('chronological' if dated else 'input order (one or more dates missing)'), '']
    for _, mid, message, values, texts, attachments in records:
        lines += [f'## Message {mid}', '', f'- Source: https://mail.google.com/mail/#all/{mid}']
        for key in ('from', 'to', 'cc', 'bcc', 'date', 'subject', 'message-id', 'in-reply-to', 'references'):
            if key in values:
                lines.append(f'- {key}: {safe(values[key])}')
        if field(message, 'internalDate', 'internal_date') is not None:
            lines.append(f'- internal_date: {safe(field(message, "internalDate", "internal_date"))}')
        for fmt, text in texts:
            lines += ['', f'Body ({fmt}):', '', fence(text)]
        if not texts:
            lines += ['', 'No inline text body supplied.']
        if attachments:
            lines += ['', 'Attachments / other MIME parts (content not exported):']
            lines.extend('- ' + safe(json.dumps(item, ensure_ascii=False, sort_keys=True)) for item in attachments)
        lines.append('')
    return tid, '\n'.join(lines)


def convert(data):
    if isinstance(data, dict) and 'threads' in data:
        require_complete(data)
        data = data['threads']
    threads = data if isinstance(data, list) else [data]
    if not threads:
        raise ThreadFormatError('no threads supplied')
    rendered = {}
    for thread in threads:
        tid, text = render_thread(thread)
        if tid in rendered:
            raise ThreadFormatError(f'duplicate thread id: {tid}')
        rendered[tid] = text
    return rendered


def thread_paths(output_dir, tids, *, overwrite=False, source=None):
    """Return threads/ID.md destinations, refusing symlinks, the source and unrequested overwrites."""
    output = Path(output_dir).resolve() / 'threads'
    if output.is_symlink():
        raise ThreadFormatError('threads directory must not be a symlink')
    paths = [output / f'{identifier(tid, "thread id")}.md' for tid in tids]
    for path in paths:
        if path.is_symlink() or (source is not None and (
                path.resolve() == Path(source).resolve()
                or (path.exists() and path.samefile(source)))):
            raise ThreadFormatError(f'refusing to replace source or symlink: {path}')
        if path.exists() and not overwrite:
            raise ThreadFormatError(f'output exists: {path}; use --overwrite')
    return output, paths


def write_threads(rendered, output_dir, *, overwrite=False, source=None):
    """Write each rendered thread to output_dir/threads/ID.md after checking every destination."""
    output, paths = thread_paths(output_dir, rendered, overwrite=overwrite, source=source)
    output.mkdir(parents=True, exist_ok=True)
    for path, text in zip(paths, rendered.values()):
        with path.open('w' if overwrite else 'x', encoding='utf-8') as stream:
            stream.write(text)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path, help='single thread, thread array, or {"threads": [...]} JSON')
    parser.add_argument('--output-dir', required=True, type=Path, help='write threads/ID.md beneath this directory')
    parser.add_argument('--overwrite', action='store_true', help='replace existing thread exports')
    args = parser.parse_args(argv)
    try:
        rendered = convert(json.loads(args.input.read_text(encoding='utf-8')))
        output = write_threads(rendered, args.output_dir, overwrite=args.overwrite, source=args.input)
        print(f'Exported {len(rendered)} threads to {output}', file=sys.stderr)
        return 0
    except (OSError, ValueError, TypeError, AttributeError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    raise SystemExit(main())
