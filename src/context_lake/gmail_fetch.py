#!/usr/bin/env python3
"""Fetch Gmail threads with the Gmail API (OAuth, read-only) and export them as Markdown.

  qcl gmail setup                                  explain the one-time Google setup
  qcl gmail login --client-secrets client.json     authorize qcl in a browser, once
  qcl gmail fetch --query newer_than:2d --output-dir DIR [--overwrite]
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import __version__
from .email_threads_to_markdown import ThreadFormatError, convert, headers, identifier, thread_paths, \
    write_threads

SCOPE = 'https://www.googleapis.com/auth/gmail.readonly'
AUTH_URI = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URI = 'https://oauth2.googleapis.com/token'
API_ROOT = 'https://gmail.googleapis.com/gmail/v1/users/me'
CREDENTIALS_ENV = 'QCL_GMAIL_CREDENTIALS'
USER_AGENT = f'quilt-context-lake/{__version__}'
TIMEOUT = 60.0
MAX_RETRIES = 5
RETRY_STATUSES = {429, 500, 502, 503, 504}
RATE_LIMIT_REASONS = {'rateLimitExceeded', 'userRateLimitExceeded'}

QUICKSTART = 'https://developers.google.com/workspace/gmail/api/quickstart/python'
ENABLE_API = 'https://console.cloud.google.com/apis/enableflow;apiid=gmail.googleapis.com'
TOKEN_EXPIRY = 'https://developers.google.com/identity/protocols/oauth2#expiration'
# (prompt key, short label, console page, explanation) in the order setup happens.
SETUP_PAGES = (
    ('1', 'Enable the Gmail API', ENABLE_API,
     'Enable the Gmail API in a Google Cloud project (create one if asked).'),
    ('2', 'Configure the OAuth consent screen', 'https://console.developers.google.com/auth/branding',
     'Configure the OAuth consent screen. For a Google Workspace account, set\n'
     '     Audience to "Internal". An "External" app left in "Testing" gets\n'
     '     authorizations that expire after 7 days, which would quietly break a\n'
     '     daily job.'),
    ('3', 'Create a Desktop app OAuth client', 'https://console.developers.google.com/auth/clients',
     'Create an OAuth client of type "Desktop app" and download its JSON file.'),
    ('g', "Google's step-by-step guide", QUICKSTART,
     'Google\'s walkthrough of steps 1-3 (ignore its Python code; qcl does that part).'),
)


class GmailError(RuntimeError):
    """A Gmail or Google OAuth request failed; messages never include tokens."""


class NotConfigured(GmailError):
    """No usable credentials; the caller should show the setup guide."""


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so a bearer token is never replayed to another host."""

    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
        return None


DEFAULT_OPENER = urllib.request.build_opener(NoRedirectHandler()).open


# --- Credentials -----------------------------------------------------------------------

def default_credentials_path():
    if os.environ.get(CREDENTIALS_ENV):
        return Path(os.environ[CREDENTIALS_ENV]).expanduser()
    base = os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config'
    return Path(base) / 'qcl' / 'gmail.json'


def load_client_secrets(path):
    """Read the JSON Google gives you for a Desktop app OAuth client."""
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise GmailError(f'cannot read client secrets {path}: {error}') from error
    if isinstance(data, dict) and 'web' in data and 'installed' not in data:
        raise GmailError(f'{path} is a "Web application" client; create a "Desktop app" client instead')
    client = data.get('installed') if isinstance(data, dict) else None
    if not isinstance(client, dict) or not all(
            isinstance(client.get(key), str) and client[key] for key in ('client_id', 'client_secret')):
        raise GmailError(f'{path} is not a Desktop app OAuth client file (expected "installed" '
                         'with client_id and client_secret)')
    return {'client_id': client['client_id'], 'client_secret': client['client_secret']}


def load_credentials(path):
    path = Path(path)
    if not path.exists():
        raise NotConfigured(f'no Gmail credentials at {path}')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise GmailError(f'cannot read Gmail credentials {path}: {error}') from error
    required = ('client_id', 'client_secret', 'refresh_token')
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data[k] for k in required):
        raise NotConfigured(f'{path} is missing client_id, client_secret or refresh_token')
    return data


def save_credentials(path, data):
    """Write credentials readable only by the current user, replacing atomically."""
    path = Path(path)
    if path.is_symlink():
        raise GmailError(f'refusing to write credentials through a symlink: {path}')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{secrets.token_hex(4)}.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write('\n')
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


# --- HTTP ------------------------------------------------------------------------------

def _error_detail(error):
    """Return (message, reasons) from a Google error body without echoing request data."""
    try:
        body = json.loads(error.read() or b'{}')
    except ValueError:
        return f'HTTP {error.code}', set()
    detail = body.get('error')
    if isinstance(detail, dict):
        reasons = {item.get('reason') for item in detail.get('errors', []) if isinstance(item, dict)}
        if detail.get('status'):
            reasons.add(detail['status'])
        return detail.get('message') or f'HTTP {error.code}', reasons
    if isinstance(detail, str):  # OAuth token endpoint: {"error": "invalid_grant", ...}
        description = body.get('error_description')
        return f'{detail}: {description}' if description else detail, {detail}
    return f'HTTP {error.code}', set()


def post_form(url, form, *, opener=DEFAULT_OPENER):
    request = urllib.request.Request(
        url, data=urllib.parse.urlencode(form).encode(), method='POST',
        headers={'Content-Type': 'application/x-www-form-urlencoded',
                 'Accept': 'application/json', 'User-Agent': USER_AGENT})
    try:
        with opener(request, timeout=TIMEOUT) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        message, reasons = _error_detail(error)
        if 'invalid_grant' in reasons:
            raise NotConfigured(
                'Google rejected the saved authorization (invalid_grant). It was revoked, or it '
                'expired because the app is "External" in "Testing" status (7-day limit, see '
                f'{TOKEN_EXPIRY}). Run `qcl gmail login` again.') from None
        if 'invalid_client' in reasons:
            raise NotConfigured(f'Google rejected the OAuth client ({message}); it may have been '
                                'deleted. Create a new Desktop app client and log in again.') from None
        raise GmailError(f'Google OAuth request failed: {message}') from None
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise GmailError(f'Google OAuth request failed: {error}') from None


def access_token(credentials, *, opener=DEFAULT_OPENER):
    tokens = post_form(credentials.get('token_uri') or TOKEN_URI, {
        'client_id': credentials['client_id'], 'client_secret': credentials['client_secret'],
        'refresh_token': credentials['refresh_token'], 'grant_type': 'refresh_token'}, opener=opener)
    if not isinstance(tokens.get('access_token'), str):
        raise GmailError('Google token response had no access_token')
    granted = tokens.get('scope', SCOPE).split()
    if SCOPE not in granted and 'https://mail.google.com/' not in granted:
        raise NotConfigured('the saved authorization does not include Gmail read access; '
                            'run `qcl gmail login` again')
    return tokens['access_token']


class GmailClient:
    """Authenticated GETs against the Gmail API with bounded retries for rate limits."""

    def __init__(self, token, *, opener=DEFAULT_OPENER, sleep=time.sleep, max_retries=MAX_RETRIES):
        self.token, self.opener, self.sleep, self.max_retries = token, opener, sleep, max_retries

    def get(self, path, params=None):
        query = f'?{urllib.parse.urlencode(params)}' if params else ''
        request = urllib.request.Request(
            f'{API_ROOT}{path}{query}', method='GET',
            headers={'Authorization': f'Bearer {self.token}', 'Accept': 'application/json',
                     'User-Agent': USER_AGENT})
        for attempt in range(self.max_retries + 1):
            try:
                with self.opener(request, timeout=TIMEOUT) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as error:
                retry_after = error.headers.get('Retry-After') if error.headers else None
                message, reasons = _error_detail(error)
                retryable = error.code in RETRY_STATUSES or (
                    error.code == 403 and reasons & RATE_LIMIT_REASONS)
                if retryable and attempt < self.max_retries:
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else 2.0 ** attempt
                    self.sleep(min(delay, 60.0))
                    continue
                if 'accessNotConfigured' in reasons or 'SERVICE_DISABLED' in reasons:
                    raise GmailError(f'the Gmail API is not enabled for this Cloud project; enable it at '
                                     f'{ENABLE_API} and retry in a few minutes') from None
                if error.code == 401 or 'insufficientPermissions' in reasons:
                    raise NotConfigured(f'Gmail refused the authorization ({message}); '
                                        'run `qcl gmail login` again') from None
                raise GmailError(f'Gmail API {path} failed: {message}') from None
            except (urllib.error.URLError, OSError, ValueError) as error:
                raise GmailError(f'Gmail API {path} failed: {error}') from None
        raise AssertionError('unreachable')


# --- Login (installed-app loopback flow with PKCE) ---------------------------------------

class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path != '/':
            self.send_error(404)
            return
        self.server.result = dict(urllib.parse.parse_qsl(url.query))
        page = b'<!doctype html><meta charset="utf-8"><title>qcl</title>' \
               b'<p>qcl received the Google response. You can close this tab.</p>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, *args):
        pass


def _wait_for_redirect(server, timeout):
    deadline = time.monotonic() + timeout
    while server.result is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GmailError(f'timed out after {timeout:.0f}s waiting for Google sign-in')
        server.timeout = remaining
        server.handle_request()
    return server.result


def authorize(client, *, account=None, open_browser=True, timeout=300.0,
              opener=DEFAULT_OPENER, browse=None, out=sys.stderr):
    """Run the consent flow on a loopback port; return Google's token response and redirect URI."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=')
    state = secrets.token_urlsafe(24)
    server = HTTPServer(('127.0.0.1', 0), _CallbackHandler)
    server.result = None
    try:
        redirect = f'http://127.0.0.1:{server.server_port}/'
        params = {'client_id': client['client_id'], 'redirect_uri': redirect, 'response_type': 'code',
                  'scope': SCOPE, 'state': state, 'code_challenge': challenge.decode(),
                  'code_challenge_method': 'S256', 'access_type': 'offline', 'prompt': 'consent'}
        if account:
            params['login_hint'] = account
        url = f'{AUTH_URI}?{urllib.parse.urlencode(params)}'
        print(f'Sign in to Google and allow read-only Gmail access:\n  {url}', file=out)
        if open_browser:
            (browse or webbrowser.open)(url)
        result = _wait_for_redirect(server, timeout)
    finally:
        server.server_close()
    if result.get('state') != state:
        raise GmailError('sign-in response did not match this login attempt; try again')
    if 'error' in result:
        raise GmailError(f'Google sign-in failed: {result["error"]}')
    if not result.get('code'):
        raise GmailError('Google sign-in returned no authorization code')
    tokens = post_form(TOKEN_URI, {
        'client_id': client['client_id'], 'client_secret': client['client_secret'],
        'code': result['code'], 'code_verifier': verifier, 'grant_type': 'authorization_code',
        'redirect_uri': redirect}, opener=opener)
    if not tokens.get('refresh_token'):
        raise GmailError('Google did not return a refresh token; remove qcl at '
                         'https://myaccount.google.com/connections and log in again')
    if SCOPE not in tokens.get('scope', SCOPE).split():
        raise GmailError('read-only Gmail access was not granted; tick the Gmail checkbox when '
                         'signing in')
    return tokens


# --- Setup guide -------------------------------------------------------------------------

def setup_guide(path, reason=None):
    lines = [f'Gmail is not set up for qcl: {reason.rstrip(".")}.' if reason else 'Gmail setup for qcl', '',
             'qcl reads mail through the Gmail API, which requires OAuth; an API key cannot',
             'read a mailbox. One-time setup, about five minutes:', '']
    for key, _, url, text in SETUP_PAGES:
        if key.isdigit():
            lines += [f'  {key}. {text}', f'     {url}']
    lines += ['  4. Authorize qcl (opens your browser once):',
              '     qcl gmail login --client-secrets ~/Downloads/client_secret_XXXX.json', '',
              f'Credentials are saved, readable only by you, at {path}', '',
              f"Google's walkthrough of steps 1-3: {QUICKSTART}"]
    return '\n'.join(lines)


def offer_pages(*, read=None, browse=None, out=sys.stderr):
    """Ask which setup page to open; returns the URL opened, or None."""
    choices = {key: (label, url) for key, label, url, _ in SETUP_PAGES}
    print('', file=out)
    for key, (label, _) in choices.items():
        print(f'  [{key}] {label}', file=out)
    out.write('Open a page in your browser? Enter a choice, or press Return to skip: ')
    out.flush()
    answer = (read or sys.stdin.readline)().strip().lower()
    if answer not in choices:
        return None
    (browse or webbrowser.open)(choices[answer][1])
    return choices[answer][1]


def interactive():
    return sys.stdin.isatty() and sys.stderr.isatty()


def explain_setup(path, reason=None, *, ask=None):
    print(setup_guide(path, reason), file=sys.stderr)
    if ask is None:
        ask = interactive()
    if ask:
        offer_pages()


# --- Fetch -------------------------------------------------------------------------------

def list_thread_ids(client, query, max_threads):
    ids = {}
    page = None
    while True:
        params = {'q': query, 'maxResults': '500'}
        if page:
            params['pageToken'] = page
        body = client.get('/threads', params)
        for item in body.get('threads', []):
            ids[identifier(item.get('id') if isinstance(item, dict) else None, 'thread id')] = None
        if len(ids) > max_threads:
            raise ThreadFormatError(f'query matched more than {max_threads} threads; narrow --query '
                                    'or raise --max-threads')
        page = body.get('nextPageToken')
        if not page:
            return list(ids)


def _inline_text_bodies(client, message_id, part):
    """Fetch text bodies Gmail stored out of line, so the converter never sees a gap."""
    if not isinstance(part, dict):
        return
    for child in part.get('parts') or []:
        _inline_text_bodies(client, message_id, child)
    body = part.get('body')
    kind = str(part.get('mimeType', '')).lower()
    disposition = headers(part).get('content-disposition', '').lower()
    if (kind in ('text/plain', 'text/html') and not part.get('filename')
            and not disposition.startswith('attachment') and isinstance(body, dict)
            and body.get('attachmentId') and 'data' not in body):
        attachment = urllib.parse.quote(str(body['attachmentId']), safe='')
        data = client.get(f'/messages/{message_id}/attachments/{attachment}').get('data')
        if not isinstance(data, str):
            raise GmailError(f'Gmail returned no body data for a text part of message {message_id}')
        body['data'] = data


def fetch_thread(client, thread_id):
    thread = client.get(f'/threads/{identifier(thread_id, "thread id")}', {'format': 'full'})
    for message in thread.get('messages', []):
        if isinstance(message, dict):
            _inline_text_bodies(client, identifier(message.get('id'), 'message id'),
                                message.get('payload'))
    return thread


def save_json(path, threads, overwrite):
    path = Path(path)
    if path.is_symlink():
        raise ThreadFormatError(f'refusing to write through a symlink: {path}')
    if path.exists() and not overwrite:
        raise ThreadFormatError(f'output exists: {path}; use --overwrite')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w' if overwrite else 'x', encoding='utf-8') as stream:
        json.dump({'threads': threads}, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def fetch(args, *, opener=DEFAULT_OPENER, sleep=time.sleep):
    credentials = load_credentials(args.credentials)
    client = GmailClient(access_token(credentials, opener=opener), opener=opener, sleep=sleep)
    account = credentials.get('account', 'Gmail')
    ids = list_thread_ids(client, args.query, args.max_threads)
    if not ids:
        print(f'No threads in {account} matched {args.query!r}; nothing written', file=sys.stderr)
        return 0
    thread_paths(args.output_dir, ids, overwrite=args.overwrite)  # fail before fetching bodies
    print(f'Fetching {len(ids)} threads from {account}', file=sys.stderr)
    threads = [fetch_thread(client, tid) for tid in ids]
    rendered = convert(threads)
    if args.save_json:
        save_json(args.save_json, threads, args.overwrite)
    output = write_threads(rendered, args.output_dir, overwrite=args.overwrite)
    print(f'Exported {len(rendered)} threads to {output}', file=sys.stderr)
    return 0


def login(args, *, opener=DEFAULT_OPENER):
    client = load_client_secrets(args.client_secrets)
    tokens = authorize(client, account=args.account, open_browser=not args.no_browser,
                       opener=opener)
    profile = GmailClient(tokens['access_token'], opener=opener).get('/profile')
    address = profile.get('emailAddress', '')
    if args.account and address.lower() != args.account.lower():
        raise GmailError(f'signed in as {address}, not {args.account}; nothing saved')
    save_credentials(args.credentials, {
        'type': 'authorized_user', 'client_id': client['client_id'],
        'client_secret': client['client_secret'], 'refresh_token': tokens['refresh_token'],
        'token_uri': TOKEN_URI, 'scopes': [SCOPE], 'account': address})
    print(f'Authorized read-only Gmail access for {address}; saved {args.credentials}',
          file=sys.stderr)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog='qcl gmail', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--credentials', type=Path, default=default_credentials_path(),
                        help=f'saved authorization (default: ${CREDENTIALS_ENV} or %(default)s)')
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('setup', help='explain the one-time Google Cloud setup')
    sign_in = commands.add_parser('login', help='authorize read-only Gmail access in a browser')
    sign_in.add_argument('--client-secrets', type=Path,
                         help='JSON downloaded for your "Desktop app" OAuth client')
    sign_in.add_argument('--account', help='expected Google account email (sign-in hint and check)')
    sign_in.add_argument('--no-browser', action='store_true',
                         help='print the sign-in URL instead of opening a browser')
    get = commands.add_parser('fetch', help='export threads matching a Gmail search as Markdown')
    get.add_argument('--query', required=True,
                     help='Gmail search, e.g. "newer_than:2d" or "label:customers newer_than:7d"')
    get.add_argument('--output-dir', required=True, type=Path,
                     help='write threads/ID.md beneath this directory')
    get.add_argument('--max-threads', type=int, default=200,
                     help='fail instead of exporting more than this many threads (default 200)')
    get.add_argument('--save-json', type=Path, help='also save the fetched {"threads": [...]} JSON')
    get.add_argument('--overwrite', action='store_true',
                     help='replace existing exports (needed for repeated scheduled runs)')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.action == 'setup':
            try:
                account = load_credentials(args.credentials).get('account', 'an account')
                print(f'Already authorized for {account} ({args.credentials}).\n', file=sys.stderr)
            except NotConfigured:
                pass
            explain_setup(args.credentials)
            return 0
        if args.action == 'login':
            if not args.client_secrets:
                explain_setup(args.credentials, 'login needs --client-secrets')
                return 2
            return login(args, opener=DEFAULT_OPENER)
        if args.max_threads < 1:
            raise ThreadFormatError('--max-threads must be at least 1')
        return fetch(args, opener=DEFAULT_OPENER)
    except NotConfigured as error:
        explain_setup(args.credentials, str(error))
        return 2
    except (GmailError, OSError, ValueError, TypeError, KeyError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    raise SystemExit(main())
