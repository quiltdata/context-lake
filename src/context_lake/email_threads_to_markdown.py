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
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
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


ROUTING_HEADERS = ('to', 'cc', 'bcc')


def routing_headers(payload):
    """Raw To/Cc/Bcc values, not MIME-decoded, for recipient matching.

    Decoding first would let an encoded display name such as
    "=?utf-8?q?list@example.test,_Alice?=" <private@example.test> read as two
    mailboxes; encoded words appear only in display names, so the raw value
    parses to the real addresses.
    """
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
        if name in ROUTING_HEADERS:
            result[name] = result.get(name, '') + (', ' if name in result else '') + entry['value']
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



FLAT_ADDRESS_FIELDS = (('from', 'sender', 'sender'), ('to', 'toRecipients', 'to_recipients'),
                       ('cc', 'ccRecipients', 'cc_recipients'), ('bcc', 'bccRecipients', 'bcc_recipients'))
FLAT_MARKERS = ('sender', 'plaintextBody', 'plaintext_body', 'htmlBody', 'html_body')
GROUPS_FOOTER_RE = re.compile(r'You received this message because you are subscribed to the Google Groups')
GROUPS_NAME_RE = re.compile(r'subscribed to the Google Groups "([^"]+)" group')
GROUPS_LINK_RE = re.compile(r'To view this discussion visit (https://groups\.google\.com/d/msgid/[^\s<>]+)')
QUOTE_INTRO_RE = re.compile(r'wrote:\s*$')
SIGNATURE_DELIMITERS = ('--', '-- ')
# Google Groups appends these lines after its footer; anything else after it means the
# footer is quoted or collected content (e.g. a digest) and is kept.
FOOTER_TAIL_PREFIXES = ('To unsubscribe from this group', 'To view this discussion')
# A footer line wrapped by the sender's client continues on the next line with the rest of
# Google's sentence, the unsubscribe address or the discussion URL; nothing else.
FOOTER_CONTINUATION_RE = re.compile(
    r'(?:[^"]*"\s*)?group\.'
    r'|(?:and )?stop receiving emails from it,?(?: send an email to)?(?:\s+\S+@\S+)?'
    r'|(?:send an email to\s+)?[^\s@]+@[^\s@]+\.[^\s@]+'
    r'|(?:visit\s+)?<?https://groups\.google\.com/\S+')


def footer_tail(lines):
    """Whether the lines after a Groups footer are only Google's own footer lines.

    A footer line or a wrapped continuation of one may follow; a blank line ends
    continuation, so a reply written below the footer keeps the footer in place.
    """
    previous_blank = False
    for line in lines:
        text = line.strip()
        if not text:
            previous_blank = True
            continue
        if text.startswith(FOOTER_TAIL_PREFIXES):
            previous_blank = False
            continue
        if previous_blank or not FOOTER_CONTINUATION_RE.fullmatch(text):
            return False
    return True


def is_flat(message):
    """A Gmail connector message: flattened fields and decoded bodies instead of a MIME payload."""
    return 'payload' not in message and any(key in message for key in FLAT_MARKERS)


def flat_values(message, mid):
    """Return header values for a connector message without reading its bodies."""
    values = {}
    for key, camel, snake in FLAT_ADDRESS_FIELDS:
        value = field(message, camel, snake)
        if value is None:
            continue
        value = [value] if isinstance(value, str) else value
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ThreadFormatError(f'message {mid} {key} must be a string or list of strings')
        if value:
            values[key] = ', '.join(value)
    for key in ('date', 'subject'):
        if message.get(key) is not None:
            if not isinstance(message[key], str):
                raise ThreadFormatError(f'message {mid} {key} must be a string')
            values[key] = message[key]
    return values


def flat_bodies(message, mid):
    """Return text bodies and attachment metadata for a connector message."""
    plain, markup = field(message, 'plaintextBody', 'plaintext_body'), field(message, 'htmlBody', 'html_body')
    texts = []
    # Prefer nonempty plain text, then nonempty HTML (same rule as the MIME path).
    if isinstance(plain, str) and plain.strip():
        texts = [('plain', plain)]
    elif isinstance(markup, str) and markup.strip():
        parser = VisibleHTML()
        parser.feed(markup)
        texts = [('html-derived', ''.join(parser.text).strip())]
    elif isinstance(plain, str):
        texts = [('plain', plain)]
    elif isinstance(markup, str):
        parser = VisibleHTML()
        parser.feed(markup)
        texts = [('html-derived', ''.join(parser.text).strip())]
    elif plain is not None or markup is not None:
        raise ThreadFormatError(f'message {mid} body must be a string')
    attachments = message.get('attachments') or []
    if not isinstance(attachments, list) or not all(isinstance(item, dict) for item in attachments):
        raise ThreadFormatError(f'message {mid} attachments must be a list of objects')
    return texts, [{'filename': item.get('filename') or '(unnamed)',
                            'mime_type': field(item, 'mimeType', 'mime_type'),
                            'attachment_id': item.get('id'), 'part_id': field(item, 'partId', 'part_id')}
                           for item in attachments]


def addressed_to(values, address):
    """Whether To/Cc/Bcc contain the address exactly (case-insensitive mailbox match)."""
    needle = address.casefold().strip()
    if not needle:
        return False
    for key in ('to', 'cc', 'bcc'):
        raw = values.get(key, '')
        for _, mailbox in getaddresses([raw]):
            if mailbox and mailbox.casefold() == needle:
                return True
    return False


def strip_quoted(text, *, html_derived=False):
    """Drop a Google Groups footer and a trailing quoted reply block; keep interleaved replies.

    Returns (text, groups_link). Only a block that runs to the end of the message
    and is entirely '>'-quoted (plus its 'On ... wrote:' introduction) is removed,
    so inline answers between quotes survive. Text with nothing to remove is
    returned unchanged, and if nothing would remain, the original is kept.

    For html-derived bodies, VisibleHTML has already flattened <blockquote> into
    plain lines without '>' markers, so the Groups footer rule would delete a
    bottom reply that followed quoted HTML history. Skip the footer rule there;
    only strip trailing '>'-quoted blocks that remain.
    """
    lines = text.splitlines()
    link = None
    removed = False
    if not html_derived:
        # Only a footer that ends the message is the list's own; footers inside the body
        # (a digest, a forwarded post) are content and stay.
        footer = next((i for i in range(len(lines) - 1, -1, -1)
                       if not lines[i].lstrip().startswith('>') and GROUPS_FOOTER_RE.search(lines[i])), None)
        if footer is not None and not footer_tail(lines[footer + 1:]):
            footer = None
        if footer is not None:
            # Join the footer's lines so a URL wrapped after "visit" is still found.
            match = GROUPS_LINK_RE.search(' '.join(line.strip() for line in lines[footer:]))
            link = match[1].rstrip('.') if match else None
            start = next((i for i in range(footer - 1, max(-1, footer - 5), -1) if lines[i] in SIGNATURE_DELIMITERS),
                         footer)
            lines = lines[:start]
            removed = True
    end = len(lines)
    while end and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith('>')):
        end -= 1
    if any(line.lstrip().startswith('>') for line in lines[end:]):
        intro = end
        while intro and not lines[intro - 1].strip():
            intro -= 1
        if intro and QUOTE_INTRO_RE.search(lines[intro - 1]):
            first = intro - 1
            # Clients wrap long introductions: "On Mon, ... 'Name' via list <" / "list@...> wrote:".
            if not lines[first].lstrip().startswith('On ') and first and lines[first - 1].lstrip().startswith('On '):
                first -= 1
            if lines[first].lstrip().startswith('On '):
                end = first
        lines = lines[:end]
        removed = True
    if not removed:
        return text, link
    while lines and (not lines[-1].strip() or lines[-1] in SIGNATURE_DELIMITERS):
        lines.pop()
    stripped = '\n'.join(lines)
    return (stripped if stripped.strip() else text), link


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
        raw = values['date']
        try:
            date = parsedate_to_datetime(raw)
            return date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp()
        except (TypeError, ValueError, OverflowError):
            pass
        # Gmail connector dates are ISO-8601 (e.g. 2026-08-31T05:35:27Z).
        try:
            iso = raw.replace('Z', '+00:00') if isinstance(raw, str) else raw
            date = datetime.fromisoformat(iso)
            return date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp()
        except (TypeError, ValueError, OverflowError) as error:
            raise ThreadFormatError('invalid Date header; supply internal_date for ordering') from error
    return None


def checked_list_address(address):
    """None for no filter; otherwise the stripped address, which must be nonempty.

    An empty --list (an unset shell variable, say) must not become an unfiltered export.
    """
    if address is None:
        return None
    if not isinstance(address, str) or not address.strip():
        raise ThreadFormatError('--list needs a nonempty mailing list address')
    return address.strip()


def render_thread(thread, list_address=None, strip_quotes=True, groups=None):
    """Render one thread; returns (thread id, Markdown or None when --list leaves no message).

    When given a set, `groups` collects Google Group names seen in unquoted footers,
    so callers can warn about list mail exported without a list filter.
    """
    if not isinstance(thread, dict):
        raise ThreadFormatError('each thread must be an object')
    require_complete(thread)
    tid = identifier(field(thread, 'id', 'thread_id'), 'thread id')
    messages = thread.get('messages')
    if not isinstance(messages, list) or not messages:
        raise ThreadFormatError(f'thread {tid} has no messages; snippets are insufficient')
    list_address = checked_list_address(list_address)
    seen = set()
    records = []
    supplied = 0
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
        supplied += 1
        if is_flat(message):
            values = flat_values(message, mid)
            routing = values
        else:
            payload = message.get('payload')
            if not isinstance(payload, dict):
                raise ThreadFormatError(f'message {mid} needs a full MIME payload')
            values = headers(payload)
            routing = routing_headers(payload)
        # Match recipients before reading bodies or dates, so an off-list message that
        # cannot be rendered (a separately stored body, say) never blocks the export.
        if list_address is not None and not addressed_to(routing, list_address):
            continue
        if is_flat(message):
            texts, attachments = flat_bodies(message, mid)
        else:
            texts, attachments = mime_content(payload)
        records.append((message_time(message, values), mid, message, values, texts, attachments))
        if groups is not None:
            groups.update(match[1] for _, text in texts for line in text.splitlines()
                          if not line.lstrip().startswith('>') for match in GROUPS_NAME_RE.finditer(line))
    if list_address is not None and not records:
        return tid, None
    for record in records:
        if is_flat(record[2]) and not record[4]:
            raise ThreadFormatError(f'message {record[1]} has no text body; fetch it with a body format')
    links = {}
    if strip_quotes:
        for index, (when, mid, message, values, texts, attachments) in enumerate(records):
            stripped = [(fmt, *strip_quoted(text, html_derived=(fmt == 'html-derived')))
                        for fmt, text in texts]
            links[mid] = next((link for _, _, link in stripped if link), None)
            records[index] = (when, mid, message, values, [(fmt, text) for fmt, text, _ in stripped], attachments)
    dated = all(record[0] is not None for record in records)
    if dated:
        records.sort(key=lambda record: (record[0], record[1]))
    title = records[0][3].get('subject', '(no subject)')
    lines = [f'# {safe(title)}', '', f'- Thread ID: {tid}',
             f'- Source: https://mail.google.com/mail/#all/{tid}',
             f'- Exported messages: {len(records)}']
    reported = field(thread, 'messageCount', 'message_count')
    if isinstance(reported, int) and reported != supplied:
        lines.append(f'- Gmail message count: {reported} ({supplied} supplied; drafts, trash and spam are not returned)')
    if list_address is not None:
        lines.append(f'- List filter: only messages addressed to {safe(list_address)} '
                     f'({len(records)} of {supplied} supplied kept; off-list replies omitted)')
    quoting = ('quoted reply history and Google Groups footers stripped; inline replies kept'
               if strip_quotes else 'quoted history retained')
    lines += ['- Completeness: only supplied messages; connector limits may omit older messages.',
              f'- Fidelity: {quoting}; attachments listed but not fetched; HTML-only bodies use derived visible text.',
              '- Order: ' + ('chronological' if dated else 'input order (one or more dates missing)'), '']
    for _, mid, message, values, texts, attachments in records:
        lines += [f'## Message {mid}', '', f'- Source: https://mail.google.com/mail/#all/{mid}']
        for key in ('from', 'to', 'cc', 'bcc', 'date', 'subject', 'message-id', 'in-reply-to', 'references'):
            if key in values:
                lines.append(f'- {key}: {safe(values[key])}')
        if field(message, 'internalDate', 'internal_date') is not None:
            lines.append(f'- internal_date: {safe(field(message, "internalDate", "internal_date"))}')
        if links.get(mid):
            # The link matched a whitespace- and angle-free URL pattern; escaping would break it.
            lines.append(f'- groups-link: <{links[mid]}>')
        for fmt, text in texts:
            lines += ['', f'Body ({fmt}):', '', fence(text)]
        if not texts:
            lines += ['', 'No inline text body supplied.']
        if attachments:
            lines += ['', 'Attachments / other MIME parts (content not exported):']
            lines.extend('- ' + safe(json.dumps(item, ensure_ascii=False, sort_keys=True)) for item in attachments)
        lines.append('')
    return tid, '\n'.join(lines)


def convert(data, list_address=None, strip_quotes=True, groups=None):
    if isinstance(data, dict) and 'threads' in data:
        require_complete(data)
        data = data['threads']
    threads = data if isinstance(data, list) else [data]
    if not threads:
        raise ThreadFormatError('no threads supplied')
    rendered, seen = {}, set()
    for thread in threads:
        tid, text = render_thread(thread, list_address, strip_quotes, groups)
        if tid in seen:
            raise ThreadFormatError(f'duplicate thread id: {tid}')
        seen.add(tid)
        if text is not None:
            rendered[tid] = text
    return rendered


def list_hint(groups):
    names = ', '.join(f'"{name}"' for name in sorted(groups))
    return (f'messages carry Google Groups footers ({names}) but no --list was given; off-list replies '
            'in these threads are included. Pass --list ADDRESS for a list archive')


def reconcile_filtered_output(output, keep):
    """For --list exports, remove stale thread Markdown so private mail cannot remain.

    Requires --overwrite when any existing *.md is not in the current export set;
    otherwise refuses so a prior unfiltered run cannot be published by mistake.
    """
    if not output.exists():
        return []
    stale = [path for path in sorted(output.glob('*.md')) if path.stem not in keep]
    return stale


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path, help='single thread, thread array, or {"threads": [...]} JSON')
    parser.add_argument('--output-dir', required=True, type=Path, help='write threads/ID.md beneath this directory')
    parser.add_argument('--overwrite', action='store_true', help='replace existing thread exports')
    parser.add_argument('--list', dest='list_address', metavar='ADDRESS',
                        help='keep only messages addressed (To/Cc/Bcc) to this mailing list; skip threads with none')
    parser.add_argument('--keep-quotes', action='store_true',
                        help='keep quoted reply history and Google Groups footers verbatim '
                             '(by default they are stripped, keeping inline replies)')
    args = parser.parse_args(argv)
    groups = set()
    try:
        args.list_address = checked_list_address(args.list_address)
        rendered = convert(json.loads(args.input.read_text(encoding='utf-8')), args.list_address,
                           not args.keep_quotes, groups)
        if groups and args.list_address is None:
            print(f'warning: {list_hint(groups)}', file=sys.stderr)
        root = args.output_dir.resolve()
        output = root / 'threads'
        if output.is_symlink():
            raise ThreadFormatError('threads directory must not be a symlink')
        stale = []
        if args.list_address is not None:
            stale = reconcile_filtered_output(output, rendered)
            for path in stale:
                if (path.resolve() == args.input.resolve()
                        or (path.exists() and path.samefile(args.input))):
                    raise ThreadFormatError(f'refusing to remove the input as a stale thread file: {path}')
            if stale and not args.overwrite:
                names = ', '.join(path.name for path in stale[:5])
                more = f' (+{len(stale) - 5} more)' if len(stale) > 5 else ''
                raise ThreadFormatError(
                    f'--list export found stale thread files not in this run ({names}{more}); '
                    'use --overwrite to remove them so private off-list mail cannot remain')
        paths = [(output / f'{tid}.md', text) for tid, text in rendered.items()]
        for path, _ in paths:
            if (path.resolve() == args.input.resolve() or path.is_symlink()
                    or (path.exists() and path.samefile(args.input))):
                raise ThreadFormatError(f'refusing to replace source or symlink: {path}')
            if path.exists() and not args.overwrite:
                raise ThreadFormatError(f'output exists: {path}; use --overwrite')
        output.mkdir(parents=True, exist_ok=True)
        for path, text in paths:
            with path.open('w' if args.overwrite else 'x', encoding='utf-8') as stream:
                stream.write(text)
        removed = 0
        if args.overwrite:
            # Checked against the input above, before anything was written.
            for path in stale:
                path.unlink()
                removed += 1
        note = f' (removed {removed} stale)' if removed else ''
        print(f'Exported {len(paths)} threads to {output}{note}', file=sys.stderr)
        return 0
    except (OSError, ValueError, TypeError, AttributeError) as error:
        hint = f' ({list_hint(groups)})' if groups and args.list_address is None else ''
        parser.error(f'{error}{hint}')


if __name__ == '__main__':
    raise SystemExit(main())
