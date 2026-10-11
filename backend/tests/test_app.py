import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient

from backend.app import Access, EXTRACTORS, Downloads, OneTimeFileResponse, create_app, normalize_url
from yt_dlp import YoutubeDL


def test_extractors_include_short_links_and_embedded_x_video():
    with YoutubeDL({'allowed_extractors': EXTRACTORS, 'quiet': True}) as downloader:
        assert {'TikTok', 'TikTokVM', 'Twitter', 'TwitterAmplify'} <= downloader._ies.keys()


@pytest.mark.parametrize('url', [
    'http://x.com/author/status/123', 'https://x.com.evil.test/author/status/123',
    'https://127.0.0.1/video', 'https://x.com@evil.test/author/status/123',
    'https://x.com:8080/author/status/123', 'https://x.com/author',
    'file:///tmp/test', 'https://tiktok.com/@author',
])
def test_reject_unsupported_urls(url):
    with pytest.raises(ValueError):
        normalize_url(url)


@pytest.mark.parametrize('url', [
    'https://x.com/author/status/123', 'https://twitter.com/author/status/123',
    'https://x.com/i/web/status/123',
    'https://www.tiktok.com/@author/video/123', 'https://vm.tiktok.com/ABC123/',
])
def test_strip_tracking(url):
    assert normalize_url(url + '?tracking=abc#fragment') == url


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(root=tmp_path, password='test-password-only')) as client:
        assert client.post('/api/login', json={'password': 'test-password-only'}).status_code == 204
        yield client


def wait_for_job(client):
    for _ in range(100):
        job = client.get('/api/jobs').json()[0]
        if job['status'] in {'ready', 'error'}:
            return job
        time.sleep(.01)
    pytest.fail('Job did not complete')


@pytest.fixture
def successful_download(monkeypatch):
    def download(command, **kwargs):
        template = Path(command[command.index('-o') + 1])
        (template.parent / 'video-123.mp4').write_bytes(b'video-test')
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(subprocess, 'run', download)


def test_download_is_removed_after_transfer(client, successful_download):
    url = 'https://x.com/author/status/123'
    response = client.post('/api/jobs', json={'urls': [url, url + '?t=1']})
    assert response.status_code == 202
    assert len(response.json()) == 1
    job = wait_for_job(client)
    assert job['status'] == 'ready'
    folder = client.app.state.downloads.root / job['id']
    assert folder.exists()
    response = client.get(f"/api/jobs/{job['id']}/file")
    assert response.content == b'video-test'
    assert 'attachment' in response.headers['content-disposition']
    assert response.headers['cache-control'] == 'no-store, private'
    assert response.headers['accept-ranges'] == 'none'
    assert not folder.exists()
    assert client.get(f"/api/jobs/{job['id']}/file").status_code == 404
    assert client.get('/api/jobs').json() == []


@pytest.mark.parametrize('timeout', [True, False])
def test_download_errors(client, monkeypatch, timeout):
    def fail(command, **kwargs):
        if timeout:
            raise subprocess.TimeoutExpired(command, 300)
        return subprocess.CompletedProcess(command, 1)
    monkeypatch.setattr(subprocess, 'run', fail)
    client.post('/api/jobs', json={'urls': ['https://x.com/author/status/123']})
    job = wait_for_job(client)
    assert job['status'] == 'error'
    assert job['error']
    assert client.get(f"/api/jobs/{job['id']}/file").status_code == 409
    assert not (client.app.state.downloads.root / job['id']).exists()


def test_validation_and_origin(client):
    assert client.post('/api/jobs', json={'urls': []}).status_code == 422
    assert client.post('/api/jobs', json={'urls': ['https://evil.test/']}).status_code == 422
    assert client.post('/api/jobs', json={'urls': ['https://x.com/a/status/1'] * 11}).status_code == 422
    assert client.post('/api/jobs', json={'urls': ['https://x.com/a/status/1']}, headers={'Origin': 'https://evil.test'}).status_code == 403
    assert client.get('/api/jobs/unknown/file').status_code == 404


def test_full_storage_marks_job_as_failed(client, monkeypatch):
    def full_disk(*args, **kwargs):
        raise OSError('No space left on device')
    monkeypatch.setattr(Path, 'mkdir', full_disk)
    client.post('/api/jobs', json={'urls': ['https://x.com/a/status/1']})
    job = wait_for_job(client)
    assert job['status'] == 'error'
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 204


def test_active_jobs_cannot_be_removed(client, monkeypatch):
    monkeypatch.setattr(Downloads, 'download', lambda *args: None)
    job = client.post('/api/jobs', json={'urls': ['https://x.com/a/status/1']}).json()[0]
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 409
    for _ in range(9):
        client.post('/api/jobs', json={'urls': ['https://x.com/a/status/1']})
    assert client.post('/api/jobs', json={'urls': ['https://x.com/a/status/1']}).status_code == 409


def test_browser_sessions_are_isolated(client, successful_download):
    response = client.post('/api/jobs', json={'urls': ['https://x.com/a/status/1']})
    assert 'owner' not in response.json()[0]
    job = wait_for_job(client)
    other = TestClient(client.app)
    response = other.post('/api/login', json={'password': 'test-password-only'})
    cookie = response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'SameSite=strict' in cookie
    assert other.get('/api/jobs').json() == []
    assert other.get(f"/api/jobs/{job['id']}/file").status_code == 404
    assert other.delete(f"/api/jobs/{job['id']}").status_code == 404
    other.post('/api/jobs', json={'urls': ['https://www.tiktok.com/@a/video/2']})
    assert len(other.get('/api/jobs').json()) == 1
    assert client.get('/api/jobs').json()[0]['id'] == job['id']
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 204
    assert not (client.app.state.downloads.root / job['id']).exists()


def test_secure_cookie(monkeypatch, client):
    monkeypatch.setenv('COOKIE_SECURE', 'true')
    response = client.post('/api/login', json={'password': 'test-password-only'})
    cookie = response.headers['set-cookie']
    assert '__Host-tt-session=' in cookie and 'Secure' in cookie


def test_partial_and_cross_site_requests_do_not_consume_file(client, successful_download):
    client.post('/api/jobs', json={'urls': ['https://x.com/a/status/1']})
    job = wait_for_job(client)
    url = f"/api/jobs/{job['id']}/file"
    assert client.get(url, headers={'Range': 'bytes=0-2'}).status_code == 416
    assert client.head(url).status_code in {404, 405}
    assert client.get(url, headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    assert client.get('/api/jobs').json()[0]['status'] == 'ready'


def test_expiration_removes_abandoned_files(client, successful_download):
    client.post('/api/jobs', json={'urls': ['https://x.com/a/status/1']})
    job = wait_for_job(client)
    downloads = client.app.state.downloads
    downloads.update(job['id'], expires_at=time.time() - 1)
    downloads.cleanup()
    assert client.get('/api/jobs').json() == []
    assert not (downloads.root / job['id']).exists()
    assert client.get(f"/api/jobs/{job['id']}/file").status_code == 404


def test_idle_cleanup_without_requests(client, successful_download, monkeypatch):
    monkeypatch.setattr('backend.app.SWEEP_INTERVAL', .01)
    # Wake the existing cleaner by creating a manager with the shortened interval.
    downloads = Downloads(client.app.state.downloads.root / 'idle')
    try:
        job = downloads.add('owner', ['https://x.com/a/status/1'])[0]
        for _ in range(100):
            if downloads.list('owner')[0]['status'] == 'ready':
                break
            time.sleep(.01)
        downloads.update(job['id'], expires_at=time.time() - 1)
        for _ in range(100):
            if not (downloads.root / job['id']).exists():
                break
            time.sleep(.01)
        assert not (downloads.root / job['id']).exists()
        assert not downloads.jobs
    finally:
        downloads.close()


def test_only_one_transfer_can_claim_a_file(client, successful_download):
    client.post('/api/jobs', json={'urls': ['https://x.com/a/status/1']})
    job = wait_for_job(client)
    downloads = client.app.state.downloads
    owner = client.cookies.get('tt-session')
    def claim():
        try:
            downloads.claim(job['id'], owner)
            return 200
        except Exception as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: claim(), range(2))) == [200, 409]
    downloads.update(job['id'], expires_at=time.time() - 1)
    downloads.cleanup()
    assert (downloads.root / job['id']).exists()
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 409
    downloads.finish(job['id'])


def test_cleanup_on_disconnected_transfer(client, successful_download):
    client.post('/api/jobs', json={'urls': ['https://x.com/a/status/1']})
    job = wait_for_job(client)
    downloads = client.app.state.downloads
    path = downloads.claim(job['id'], client.cookies.get('tt-session'))
    response = OneTimeFileResponse(path, downloads, job['id'])

    async def disconnected_request():
        started = anyio.Event()
        async def receive():
            await started.wait()
            return {'type': 'http.disconnect'}
        async def send(message):
            if message['type'] == 'http.response.body':
                started.set()
                await anyio.sleep_forever()
        await response({'type': 'http', 'method': 'GET', 'headers': []}, receive, send)
    anyio.run(disconnected_request)
    assert not path.parent.exists()
    assert client.get('/api/jobs').json() == []


def test_global_limit_preserves_other_sessions(client, monkeypatch):
    monkeypatch.setattr(Downloads, 'download', lambda *args: None)
    downloads = client.app.state.downloads
    for index in range(4):
        downloads.add(str(index), [f'https://x.com/a/status/{i}' for i in range(10)])
    response = client.post('/api/jobs', json={'urls': ['https://x.com/a/status/1']})
    assert response.status_code == 503
    assert len(downloads.jobs) == 40


def test_submission_rate_limit(client, successful_download):
    downloads = client.app.state.downloads
    client.get('/api/jobs')
    owner = client.cookies.get('tt-session')
    downloads.submissions.extend([(owner, time.time())] * 20)
    response = client.post('/api/jobs', json={'urls': ['https://x.com/a/status/1']})
    assert response.status_code == 429
    assert response.headers['retry-after'] == '60'


def test_password_required_and_logout_revokes_session(client):
    other = TestClient(client.app)
    assert other.get('/api/jobs').status_code == 401
    assert other.post('/api/jobs', json={'urls': ['https://x.com/a/status/1']}).status_code == 401
    assert other.post('/api/login', json={'password': 'wrong'}).status_code == 401
    token = client.cookies.get('tt-session')
    assert client.post('/api/logout').status_code == 204
    assert client.get('/api/jobs', headers={'Cookie': f'tt-session={token}'}).status_code == 401
    assert other.get('/openapi.json').status_code == 404


def test_expired_session_is_rejected(client):
    token = client.cookies.get('tt-session')
    client.app.state.access.sessions[token] = time.time() - 1
    assert client.get('/api/jobs').status_code == 401


def test_sign_in_rate_limit(client):
    other = TestClient(client.app)
    for _ in range(5):
        assert other.post('/api/login', json={'password': 'wrong'}).status_code == 401
    response = other.post('/api/login', json={'password': 'test-password-only'})
    assert response.status_code == 429
    assert response.headers['retry-after'] == '300'


def test_missing_password_fails_closed():
    with pytest.raises(RuntimeError):
        Access(None)
    with pytest.raises(RuntimeError):
        Access('x' * 257)


def test_successful_sign_ins_do_not_consume_failed_attempt_budget():
    access = Access('test-password-only')
    for _ in range(6):
        assert access.login('test-password-only', 'shared-ip')
    assert not access.attempts
