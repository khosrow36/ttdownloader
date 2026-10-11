# TTdownloader

A self-hosted video downloader for TikTok and Twitter (X), built with Vue 3, FastAPI and yt-dlp. One Python process serves the API and compiled frontend. FFmpeg merges audio and video when needed.

Access is protected by a shared password. Each browser session has its own download queue. Files are deleted after transfer, on an interrupted transfer, or after ten minutes without being downloaded.

## Run with Docker

Requires Docker Engine or Docker Desktop with Compose v2.

Create the password file once:

```sh
mkdir -p .secrets
chmod 700 .secrets
if [ ! -e .secrets/app_password ]; then
  openssl rand -base64 32 > .secrets/app_password
fi
chmod 644 .secrets/app_password
```

The directory restricts access to its owner. The file must be readable by the container's non-root user. It is excluded from Git and the Docker build context.

Read the password locally, then start the application:

```sh
cat .secrets/app_password
docker compose up --build -d
```

Open <http://localhost:8000>. The port binds to loopback only. The container uses a read-only root filesystem and a 2 GiB temporary filesystem for downloads.

```sh
docker compose logs -f
docker compose down
```

## Local development

Requires Python 3.13+, Node.js 24 and FFmpeg on `PATH`. Create the password file as described above.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements-dev.txt
npm --prefix frontend ci
.venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

In another terminal:

```sh
npm --prefix frontend run dev
```

Vite proxies `/api` requests to the backend. To serve the production frontend through FastAPI, run `npm --prefix frontend run build` and restart the backend.

## Configuration

| Variable | Purpose |
| --- | --- |
| `APP_PASSWORD_FILE` | Password file; defaults to `.secrets/app_password` outside Docker. |
| `APP_PASSWORD` | Alternative password value. Takes precedence over the file. |
| `COOKIE_SECURE` | Set to `true` behind HTTPS to require secure session cookies. |
| `FORWARDED_ALLOW_IPS` | Addresses of trusted reverse proxies. |
| `DATA_DIR` | Parent directory for temporary download storage. |
| `STATIC_DIR` | Directory containing the compiled frontend. |

Passwords must contain 12–256 characters. Sessions expire after 12 hours. Cookies use `HttpOnly` and `SameSite=Strict`; HTTPS or `COOKIE_SECURE=true` enables `Secure`. Logout revokes the session on the server.

Changing the password file requires recreating the container:

```sh
docker compose up -d --force-recreate
```

A restart invalidates all sessions and removes queued downloads and temporary files.

## Tests

```sh
.venv/bin/python -m pytest backend/tests -q
npm --prefix frontend run build
docker compose config --quiet
shellcheck deploy/*.sh
```

API tests cover authentication, session isolation, queue limits, file transfer and cleanup. Deployment tests cover successful updates and failure recovery with a simulated Docker command. Tests do not contact TikTok or Twitter and do not verify SSH access to a live server.

## CI and deployment

[The GitHub Actions workflow](.github/workflows/ci.yml) runs backend tests, ShellCheck, a production frontend build and a Docker startup check on pushes and pull requests. It can also be started manually.

Runs on `main` publish the tested image to `ghcr.io/OWNER/REPOSITORY`, tagged with the commit SHA. Deployment uses the image digest. The current runner builds a Linux AMD64 image.

SSH deployment is enabled by the repository variable `DEPLOY_ENABLED=true`. The `production` environment supplies:

| Setting | Type | Default |
| --- | --- | --- |
| `VPS_HOST` | Variable | Required |
| `VPS_USER` | Variable | Required |
| `VPS_PORT` | Variable | `22` |
| `DEPLOY_PATH` | Variable | `/opt/ttdownloader` |
| `VPS_SSH_KEY` | Secret | Required |
| `VPS_KNOWN_HOSTS` | Secret | Required; verified SSH host key entries |

The target needs Linux, Docker Compose v2, Bash and `flock`. The SSH user must be able to run Docker and write to the deployment directory. The application password stays in `DEPLOY_PATH/.secrets/app_password` on the server. GHCR packages must be public or the deployment user must already be authenticated to the registry.

[The production Compose file](deploy/compose.yaml) uses host networking and binds the application to `127.0.0.1:18000`. It requires a host-level HTTPS reverse proxy. [The Nginx configuration](deploy/nginx.conf) must be adapted to the deployment's domain and existing certificate paths before use. It disables response buffering and overwrites forwarded client headers. SELinux hosts may require `httpd_can_network_connect` for Nginx; the password mount uses the `Z` label option.

The deployment script pulls the new image before replacing the container and waits for its healthcheck. A failed startup triggers an attempt to restore the previous release. Updates cause a short interruption and clear active sessions and downloads. The healthcheck does not verify public DNS, TLS or platform availability. Old release directories and images are retained and require periodic cleanup.

## Limits

- Public TikTok videos and Twitter posts only. Private content and platform authentication are not supported. For posts containing multiple videos, only the first is downloaded.
- Two concurrent download workers, up to 10 queue entries per session and 40 per server.
- Up to 20 new downloads per minute per session and 120 per server.
- Maximum file size: 250 MiB. Download and transfer timeouts: five minutes each.
- Completed downloads and errors expire after ten minutes. A background sweep runs every 30 seconds.
- File transfers are single-use. Interrupted transfers cannot be resumed; submit the source URL again. Transfer completion does not confirm that the browser saved the file.
- Session and queue state live in memory. Run one Uvicorn worker and one application instance per data directory.
- Platform changes and rate limits can break extraction. Update the pinned yt-dlp dependency and rebuild when needed.

This application is intended for a small group sharing a password. Use HTTPS for access over the internet. Download only content you are permitted to save.
