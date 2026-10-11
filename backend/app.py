import os
import re
import secrets
import hashlib
import hmac
import shutil
import subprocess
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event, Lock, Thread
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import anyio
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

EXTRACTORS = ['TikTok', 'vm.tiktok', 'twitter.*']
FILE_TTL = 10 * 60
SESSION_LIMIT = 10
GLOBAL_LIMIT = 40
SWEEP_INTERVAL = 30
SESSION_TTL = 12 * 60 * 60


def secure_cookie(request):
    return os.environ.get('COOKIE_SECURE', '').lower() == 'true' or request.url.scheme == 'https'


def cookie_name(request):
    return '__Host-tt-session' if secure_cookie(request) else 'tt-session'


class Access:
    def __init__(self, password):
        if not password or not 12 <= len(password) <= 256:
            raise RuntimeError('Set a password of 12 to 256 characters in APP_PASSWORD_FILE or APP_PASSWORD.')
        self.password_hash = hashlib.sha256(password.encode()).digest()
        self.sessions = {}
        self.attempts = deque()
        self.lock = Lock()

    def login(self, password, address):
        with self.lock:
            now = time.time()
            while self.attempts and self.attempts[0][1] <= now - 300:
                self.attempts.popleft()
            if sum(item[0] == address for item in self.attempts) >= 5 or len(self.attempts) >= 30:
                raise HTTPException(429, 'Too many sign-in attempts. Try again in 5 minutes.', headers={'Retry-After': '300'})
            if not hmac.compare_digest(self.password_hash, hashlib.sha256(password.encode()).digest()):
                self.attempts.append((address, now))
                raise HTTPException(401, 'Incorrect password.')
            self.sessions = {token: expiry for token, expiry in self.sessions.items() if expiry > now}
            if len(self.sessions) >= 100:
                raise HTTPException(503, 'Too many active sessions. Try again later.')
            token = secrets.token_urlsafe(32)
            self.sessions[token] = now + SESSION_TTL
            return token

    def authenticated(self, token):
        with self.lock:
            if self.sessions.get(token, 0) > time.time():
                return True
            self.sessions.pop(token, None)
            return False

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token, None)


class SignIn(BaseModel):
    password: str = Field(min_length=1, max_length=256)


def normalize_url(value: str) -> str:
    try:
        url = urlsplit(value.strip())
        if url.scheme != 'https' or url.username or url.password or url.port:
            raise ValueError
        host = url.hostname
        if host in {'x.com', 'www.x.com', 'twitter.com', 'www.twitter.com'}:
            valid = re.fullmatch(r'/(?:[A-Za-z0-9_]+|i/web)/status/[0-9]+/?', url.path)
        elif host in {'tiktok.com', 'www.tiktok.com'}:
            valid = re.fullmatch(r'/@[^/]+/video/[0-9]+/?|/t/[A-Za-z0-9]+/?', url.path)
        elif host in {'vm.tiktok.com', 'vt.tiktok.com'}:
            valid = re.fullmatch(r'/[A-Za-z0-9]+/?', url.path)
        else:
            valid = False
        if not valid:
            raise ValueError
        return urlunsplit(('https', host, url.path, '', ''))
    except ValueError:
        raise ValueError('Use an HTTPS link to a TikTok video or Twitter post.') from None


class Batch(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=10)

    @field_validator('urls')
    @classmethod
    def validate_urls(cls, values):
        if any(len(value) > 2048 for value in values):
            raise ValueError('This link is too long.')
        return list(dict.fromkeys(normalize_url(value) for value in values))


class Downloads:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs = {}
        self.lock = Lock()
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.submissions = deque()
        self.stop = Event()
        self.cleaner = Thread(target=self.sweep, daemon=True)
        self.cleaner.start()

    @staticmethod
    def public(job):
        return {key: value for key, value in job.items() if key != 'owner'}

    def list(self, owner):
        self.cleanup()
        with self.lock:
            return [self.public(job) for job in reversed(list(self.jobs.values()))
                    if job['owner'] == owner]

    def update(self, job_id, **values):
        with self.lock:
            self.jobs[job_id].update(values)
            if values.get('status') in {'ready', 'error'}:
                self.jobs[job_id]['expires_at'] = time.time() + FILE_TTL

    def add(self, owner, urls):
        self.cleanup()
        with self.lock:
            now = time.time()
            while self.submissions and self.submissions[0][1] <= now - 60:
                self.submissions.popleft()
            if (sum(item[0] == owner for item in self.submissions) + len(urls) > 20
                    or len(self.submissions) + len(urls) > 120):
                raise HTTPException(429, 'Too many downloads. Try again in a minute.', headers={'Retry-After': '60'})
            if sum(job['owner'] == owner for job in self.jobs.values()) + len(urls) > SESSION_LIMIT:
                raise HTTPException(409, 'Your queue is full. Save or remove finished downloads first.')
            if len(self.jobs) + len(urls) > GLOBAL_LIMIT:
                raise HTTPException(503, 'The server is busy. Try again shortly.', headers={'Retry-After': '30'})
            added = []
            for url in urls:
                job = {'id': uuid4().hex, 'url': url, 'status': 'queued',
                       'platform': 'TikTok' if 'tiktok.com' in urlsplit(url).hostname else 'X',
                       'error': None, 'filename': None, 'owner': owner, 'expires_at': None}
                self.jobs[job['id']] = job
                self.submissions.append((owner, now))
                added.append(self.public(job))
        for job in added:
            self.pool.submit(self.download, job['id'], job['url'])
        return added

    def download(self, job_id, url):
        folder = self.root / job_id
        self.update(job_id, status='downloading')
        try:
            folder.mkdir()
            result = subprocess.run([
                sys.executable, '-m', 'yt_dlp', '--ignore-config',
                '--use-extractors', ','.join(EXTRACTORS), '--no-playlist',
                '--playlist-items', '1', '--no-progress', '--no-warnings',
                '--socket-timeout', '20', '--retries', '2',
                '--max-filesize', '250M', '--restrict-filenames',
                '-f', 'bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b',
                '--merge-output-format', 'mp4',
                '-o', str(folder / '%(title).100B-%(id)s.%(ext)s'), url,
            ], capture_output=True, timeout=300)
            files = [file for file in folder.iterdir()
                     if file.suffix in {'.mp4', '.webm', '.mkv', '.mov'}]
            if result.returncode or len(files) != 1:
                raise RuntimeError('download failed')
            if files[0].stat().st_size > 250 * 1024 * 1024:
                raise RuntimeError('file too large')
            self.update(job_id, status='ready', filename=files[0].name)
        except subprocess.TimeoutExpired:
            self.update(job_id, status='error', error='Download timed out after 5 minutes. Try again.')
            shutil.rmtree(folder, ignore_errors=True)
        except Exception:
            self.update(job_id, status='error', error='Download failed. The video may be unavailable, require login, or exceed 250 MB.')
            shutil.rmtree(folder, ignore_errors=True)

    def owned(self, job_id, owner):
        job = self.jobs.get(job_id)
        if job is None or job['owner'] != owner:
            raise HTTPException(404, 'Download not found.')
        return job

    def claim(self, job_id, owner):
        self.cleanup()
        with self.lock:
            job = self.owned(job_id, owner)
            if job['status'] != 'ready':
                raise HTTPException(409, 'This file is not available for download.')
            job['status'] = 'sending'
            return self.root / job_id / job['filename']

    def delete(self, job_id, owner):
        with self.lock:
            job = self.owned(job_id, owner)
            if job['status'] in {'queued', 'downloading', 'sending'}:
                raise HTTPException(409, 'Wait for the download to finish before removing it.')
            self.jobs.pop(job_id)
        shutil.rmtree(self.root / job_id, ignore_errors=True)

    def finish(self, job_id):
        with self.lock:
            self.jobs.pop(job_id, None)
        shutil.rmtree(self.root / job_id, ignore_errors=True)

    def cleanup(self):
        with self.lock:
            expired = [job_id for job_id, job in self.jobs.items()
                       if job['status'] in {'ready', 'error'} and job['expires_at'] <= time.time()]
            for job_id in expired:
                del self.jobs[job_id]
            while self.submissions and self.submissions[0][1] <= time.time() - 60:
                self.submissions.popleft()
        for job_id in expired:
            shutil.rmtree(self.root / job_id, ignore_errors=True)

    def sweep(self):
        while not self.stop.wait(SWEEP_INTERVAL):
            self.cleanup()

    def close(self):
        self.stop.set()
        self.cleaner.join()
        self.pool.shutdown(wait=True, cancel_futures=True)
        shutil.rmtree(self.root, ignore_errors=True)


class OneTimeFileResponse(FileResponse):
    def __init__(self, path, downloads, job_id):
        super().__init__(path, filename=path.name, headers={'Accept-Ranges': 'none'})
        self.downloads = downloads
        self.job_id = job_id

    async def __call__(self, scope, receive, send):
        async def watch_disconnect(group):
            while True:
                if (await receive())['type'] == 'http.disconnect':
                    group.cancel_scope.cancel()
                    return

        # Force chunked file reads: sendfile offloading may return before the transfer finishes.
        scope = dict(scope, extensions={key: value for key, value in scope.get('extensions', {}).items()
                                       if key != 'http.response.pathsend'})
        try:
            with anyio.fail_after(300):
                async with anyio.create_task_group() as group:
                    group.start_soon(watch_disconnect, group)
                    await super().__call__(scope, receive, send)
                    group.cancel_scope.cancel()
        finally:
            self.downloads.finish(self.job_id)


def create_app(root=None, static=None, password=None):
    @asynccontextmanager
    async def lifespan(app):
        configured_password = password or os.environ.get('APP_PASSWORD')
        if configured_password is None:
            password_file = Path(os.environ.get('APP_PASSWORD_FILE', '.secrets/app_password'))
            if password_file.is_file():
                configured_password = password_file.read_text().strip()
        app.state.access = Access(configured_password)
        session = Path(root or os.environ.get('DATA_DIR', 'data')) / 'ttdownloader-session'
        shutil.rmtree(session, ignore_errors=True)
        app.state.downloads = Downloads(session)
        try:
            yield
        finally:
            await anyio.to_thread.run_sync(app.state.downloads.close)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def session_and_origin(request: Request, call_next):
        if not request.url.path.startswith('/api/') or request.url.path == '/api/health':
            return await call_next(request)
        origin = request.headers.get('origin')
        if (request.headers.get('sec-fetch-site') == 'cross-site'
                or (origin and urlsplit(origin).netloc != request.headers.get('host'))):
            return JSONResponse({'detail': 'Request origin not allowed.'}, status_code=403)
        owner = request.cookies.get(cookie_name(request), '')
        if request.url.path != '/api/login' and not request.app.state.access.authenticated(owner):
            return JSONResponse({'detail': 'Sign in to continue.'}, status_code=401,
                                headers={'Cache-Control': 'no-store, private'})
        request.state.owner = owner
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store, private'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.post('/api/login', status_code=204)
    def login(body: SignIn, request: Request, response: Response):
        address = request.client.host if request.client else 'unknown'
        token = request.app.state.access.login(body.password, address)
        request.app.state.access.logout(request.state.owner)
        response.set_cookie(cookie_name(request), token, httponly=True,
                            secure=secure_cookie(request), samesite='strict', max_age=SESSION_TTL)

    @app.post('/api/logout', status_code=204)
    def logout(request: Request, response: Response):
        request.app.state.access.logout(request.state.owner)
        response.delete_cookie(cookie_name(request), httponly=True,
                               secure=secure_cookie(request), samesite='strict')

    @app.get('/api/health')
    def health():
        return {'status': 'ok'}

    @app.get('/api/jobs')
    def jobs(request: Request):
        return request.app.state.downloads.list(request.state.owner)

    @app.post('/api/jobs', status_code=202)
    def add(batch: Batch, request: Request):
        return request.app.state.downloads.add(request.state.owner, batch.urls)

    @app.delete('/api/jobs/{job_id}', status_code=204)
    def delete(job_id: str, request: Request):
        request.app.state.downloads.delete(job_id, request.state.owner)

    @app.get('/api/jobs/{job_id}/file')
    def file(job_id: str, request: Request):
        downloads = request.app.state.downloads
        if request.headers.get('range'):
            raise HTTPException(416, 'Partial downloads are not supported. Download the complete file.')
        path = downloads.claim(job_id, request.state.owner)
        return OneTimeFileResponse(path, downloads, job_id)

    static_path = Path(static or os.environ.get('STATIC_DIR', 'frontend/dist'))
    if static_path.is_dir():
        app.mount('/', StaticFiles(directory=static_path, html=True), name='frontend')
    return app


app = create_app()
