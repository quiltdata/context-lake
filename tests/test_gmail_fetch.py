"""Offline checks for `qcl gmail`: setup guidance, OAuth handling and thread export."""
import base64
import io
import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from context_lake import cli
from context_lake import gmail_fetch as gmail


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip('=')


def full_thread(tid='t1', text='Hello from the API'):
    return {'id': tid, 'messages': [{
        'id': f'{tid}m1', 'threadId': tid, 'internalDate': '1000',
        'payload': {'mimeType': 'multipart/alternative', 'headers': [
            {'name': 'Subject', 'value': 'Status'}, {'name': 'From', 'value': 'a@example.test'}],
            'parts': [{'mimeType': 'text/plain', 'body': {'size': 5, 'attachmentId': 'big/1'}},
                      {'mimeType': 'text/html', 'body': {'data': b64('<p>html</p>')}}]}}]}


class FakeGoogle:
    """Route requests by URL; record them so tests can check what was sent."""

    def __init__(self, threads=(), pages=None, errors=None):
        self.threads = {t['id']: t for t in threads}
        self.pages = pages or [[t['id'] for t in threads]]
        self.errors = dict(errors or {})
        self.requests = []

    def __call__(self, request, timeout=None):
        url = urllib.parse.urlsplit(request.full_url)
        query = dict(urllib.parse.parse_qsl(url.query))
        self.requests.append((request.get_method(), url.path, query, request))
        for prefix, (code, body) in list(self.errors.items()):
            if url.path.startswith(prefix):
                if isinstance(code, list):  # a sequence of failures, then success
                    if not code:
                        continue
                    code = code.pop(0)
                raise urllib.error.HTTPError(request.full_url, code, 'error', {},
                                             io.BytesIO(json.dumps(body).encode()))
        if url.netloc == 'oauth2.googleapis.com':
            return self.reply({'access_token': 'access', 'scope': gmail.SCOPE,
                               'refresh_token': 'refresh'})
        path = url.path.removeprefix('/gmail/v1/users/me')
        if path == '/profile':
            return self.reply({'emailAddress': 'me@example.test'})
        if path == '/threads':
            index = int(query.get('pageToken', 0))
            body = {'threads': [{'id': tid} for tid in self.pages[index]]}
            if index + 1 < len(self.pages):
                body['nextPageToken'] = str(index + 1)
            return self.reply(body)
        if path.startswith('/threads/'):
            return self.reply(json.loads(json.dumps(self.threads[path.split('/')[-1]])))
        if '/attachments/' in path:
            return self.reply({'data': b64('Plain body fetched separately')})
        raise AssertionError(f'unexpected request {url.geturl()}')

    @staticmethod
    def reply(body):
        return io.BytesIO(json.dumps(body).encode())


@pytest.fixture
def creds(tmp_path):
    path = tmp_path / 'config' / 'gmail.json'
    gmail.save_credentials(path, {'client_id': 'id', 'client_secret': 'secret',
                                  'refresh_token': 'refresh-secret-value', 'account': 'me@example.test'})
    return path


def fetch_args(creds, out, *extra):
    return gmail.build_parser().parse_args(
        ['--credentials', str(creds), 'fetch', '--query', 'newer_than:2d',
         '--output-dir', str(out), *extra])


def test_missing_credentials_explain_setup_with_google_links(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(gmail, 'interactive', lambda: False)
    code = cli.main(['gmail', '--credentials', str(tmp_path / 'none.json'), 'fetch',
                     '--query', 'x', '--output-dir', str(tmp_path / 'out')])
    err = capsys.readouterr().err
    assert code == 2
    assert 'Gmail is not set up for qcl: no Gmail credentials at' in err
    for url in (gmail.ENABLE_API, 'https://console.developers.google.com/auth/branding',
                'https://console.developers.google.com/auth/clients', gmail.QUICKSTART):
        assert url in err
    assert '"Internal"' in err and 'qcl gmail login --client-secrets' in err
    assert 'Open a page' not in err  # never prompts when not attached to a terminal
    assert not (tmp_path / 'out').exists()


def test_interactive_guide_opens_the_chosen_page(tmp_path, capsys, monkeypatch):
    opened = []
    monkeypatch.setattr(gmail, 'interactive', lambda: True)
    monkeypatch.setattr(gmail.webbrowser, 'open', opened.append)
    monkeypatch.setattr(gmail.sys.stdin, 'readline', lambda: '3\n')
    assert gmail.main(['--credentials', str(tmp_path / 'none.json'), 'login']) == 2
    assert opened == ['https://console.developers.google.com/auth/clients']
    assert 'login needs --client-secrets' in capsys.readouterr().err


def test_interactive_guide_skips_on_return(monkeypatch, capsys):
    opened = []
    assert gmail.offer_pages(read=lambda: '\n', browse=opened.append) is None
    assert opened == []


def test_fetch_exports_markdown_and_inlines_out_of_line_text(creds, tmp_path, capsys):
    google = FakeGoogle([full_thread('t1'), full_thread('t2')], pages=[['t1'], ['t2']])
    out = tmp_path / 'out'
    assert gmail.fetch(fetch_args(creds, out, '--save-json', str(tmp_path / 'raw.json')),
                       opener=google) == 0
    text = (out / 'threads' / 't1.md').read_text()
    assert 'Plain body fetched separately' in text and 'Body (plain)' in text
    assert (out / 'threads' / 't2.md').exists()
    assert len(json.loads((tmp_path / 'raw.json').read_text())['threads']) == 2
    _, path, query, request = google.requests[1]
    assert path.endswith('/threads') and query['q'] == 'newer_than:2d'
    assert request.get_header('Authorization') == 'Bearer access'
    thread_query = google.requests[3][2]
    assert thread_query == {'format': 'full'}
    assert 'Exported 2 threads' in capsys.readouterr().err


def test_repeat_run_needs_overwrite_and_fails_before_fetching_bodies(creds, tmp_path):
    out = tmp_path / 'out'
    gmail.fetch(fetch_args(creds, out), opener=FakeGoogle([full_thread()]))
    again = FakeGoogle([full_thread(text='new')])
    with pytest.raises(ValueError, match='use --overwrite'):
        gmail.fetch(fetch_args(creds, out), opener=again)
    assert not any(path.startswith('/gmail/v1/users/me/threads/') for _, path, _, _ in again.requests)
    assert gmail.fetch(fetch_args(creds, out, '--overwrite'), opener=again) == 0


def test_no_matches_writes_nothing(creds, tmp_path, capsys):
    assert gmail.fetch(fetch_args(creds, tmp_path / 'out'), opener=FakeGoogle(pages=[[]])) == 0
    assert not (tmp_path / 'out').exists()
    assert 'nothing written' in capsys.readouterr().err


def test_too_many_threads_fails_instead_of_truncating(creds, tmp_path):
    google = FakeGoogle([full_thread('a'), full_thread('b')])
    with pytest.raises(ValueError, match='more than 1 threads'):
        gmail.fetch(fetch_args(creds, tmp_path / 'out', '--max-threads', '1'), opener=google)


def test_revoked_token_points_back_to_login(creds, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(gmail, 'interactive', lambda: False)
    monkeypatch.setattr(gmail, 'DEFAULT_OPENER', FakeGoogle(errors={'/token': (400, {
        'error': 'invalid_grant', 'error_description': 'Token has been expired or revoked.'})}))
    assert gmail.main(['--credentials', str(creds), 'fetch', '--query', 'x',
                       '--output-dir', str(tmp_path / 'out')]) == 2
    err = capsys.readouterr().err
    assert 'invalid_grant' in err and 'qcl gmail login' in err and gmail.TOKEN_EXPIRY in err
    assert 'refresh-secret-value' not in err


def test_rate_limits_are_retried_and_disabled_api_is_explained(creds, tmp_path):
    slept = []
    google = FakeGoogle([full_thread()], errors={
        '/gmail/v1/users/me/threads/t1': ([429, 503], {'error': {'message': 'slow down'}})})
    client = gmail.GmailClient('access', opener=google, sleep=slept.append)
    assert client.get('/threads/t1')['id'] == 't1'
    assert slept == [1.0, 2.0]
    disabled = FakeGoogle(errors={'/gmail': (403, {'error': {
        'message': 'Gmail API has not been used', 'errors': [{'reason': 'accessNotConfigured'}]}})})
    with pytest.raises(gmail.GmailError, match='enableflow'):
        gmail.GmailClient('access', opener=disabled).get('/profile')


def test_client_secrets_must_be_desktop_app(tmp_path):
    web = tmp_path / 'web.json'
    web.write_text(json.dumps({'web': {'client_id': 'a', 'client_secret': 'b'}}))
    with pytest.raises(gmail.GmailError, match='Desktop app'):
        gmail.load_client_secrets(web)
    desktop = tmp_path / 'desktop.json'
    desktop.write_text(json.dumps({'installed': {'client_id': 'a', 'client_secret': 'b'}}))
    assert gmail.load_client_secrets(desktop) == {'client_id': 'a', 'client_secret': 'b'}


def test_credentials_are_private(creds):
    assert creds.stat().st_mode & 0o077 == 0
    assert gmail.load_credentials(creds)['refresh_token'] == 'refresh-secret-value'


def test_login_flow_uses_loopback_pkce_and_saves_account(tmp_path, capsys):
    secrets_file = tmp_path / 'client.json'
    secrets_file.write_text(json.dumps({'installed': {'client_id': 'id', 'client_secret': 's'}}))
    google = FakeGoogle()

    def browser(url):
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        assert params['scope'] == gmail.SCOPE and params['code_challenge_method'] == 'S256'
        assert params['access_type'] == 'offline' and params['login_hint'] == 'me@example.test'
        callback = f"{params['redirect_uri']}?code=abc&state={params['state']}"
        threading.Thread(target=lambda: urllib.request.urlopen(callback).read()).start()

    client = gmail.load_client_secrets(secrets_file)
    tokens = gmail.authorize(client, account='me@example.test', browse=browser, opener=google,
                             timeout=10)
    assert tokens['refresh_token'] == 'refresh'
    form = dict(urllib.parse.parse_qsl(google.requests[0][3].data.decode()))
    assert form['code'] == 'abc' and form['code_verifier'] and form['redirect_uri'].startswith(
        'http://127.0.0.1:')


def test_login_rejects_a_different_account(tmp_path, monkeypatch):
    secrets_file = tmp_path / 'client.json'
    secrets_file.write_text(json.dumps({'installed': {'client_id': 'id', 'client_secret': 's'}}))
    monkeypatch.setattr(gmail, 'authorize', lambda *a, **k: {'access_token': 'a',
                                                             'refresh_token': 'r'})
    args = gmail.build_parser().parse_args(
        ['--credentials', str(tmp_path / 'c.json'), 'login', '--client-secrets', str(secrets_file),
         '--account', 'other@example.test'])
    with pytest.raises(gmail.GmailError, match='nothing saved'):
        gmail.login(args, opener=FakeGoogle())
    assert not (tmp_path / 'c.json').exists()
    args.account = None
    assert gmail.login(args, opener=FakeGoogle()) == 0
    assert json.loads((tmp_path / 'c.json').read_text())['account'] == 'me@example.test'
