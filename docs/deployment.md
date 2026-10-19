# Deployment

How to run Capium Time & Fees on a container platform (Coolify, Dokku, Render, a plain
Docker host). Two repositories, three containers:

| Container | Repository | Build | Port | Health check |
|---|---|---|---|---|
| API | `capiumtimenfeeapi` | `Dockerfile` (default, last stage `api`) | 8000 | `GET /health/ready` (`/health/live` for liveness) |
| Worker | `capiumtimenfeeapi` | `Dockerfile` target `worker`, or `Dockerfile.worker` | none | process running |
| UI | `capiumtimenfeeui` | `Dockerfile` | 80 | `GET /healthz` |

`docker-compose.dev.yml` is the **local development stack** (its own Postgres, Mailpit and
development secrets). Never deploy it: choose the *Dockerfile* build type on the platform.

## Request path

```text
Browser ──HTTPS──► UI (nginx) ──/api/*──► API ──► PostgreSQL
                     └── static app
```

The browser only ever talks to the UI's origin. nginx forwards `/api` to the API
(`API_UPSTREAM`), so the refresh-token cookie is first-party and no CORS preflight is needed.
The API checks the `Origin` of the refresh and logout calls against `CORS_ORIGINS`.

## API environment

| Variable | Example | Notes |
|---|---|---|
| `APP_ENV` | `production` | Enables the production checks and hides `/docs` |
| `APP_DEBUG` | `false` | Must be false in production |
| `DB_URL` | `postgresql://user:pass@host:5432/db` | Add `?sslmode=require` when the server supports TLS |
| `JWT_SECRET_KEY` | 48+ random characters | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `FRONTEND_URL` | `https://timenfee.example.com` | The UI's public origin (links in emails, secure cookies) |
| `BACKEND_URL` | `https://api.timenfee.example.com` | The API's public origin |
| `CORS_ORIGINS` | `https://timenfee.example.com` | Comma-separated; every origin the UI is served from |
| `CLOSED_DEPLOYMENT` | `true` | One practice; staff accounts made by the admin |
| `SMTP_HOST` | empty | Empty selects the mock messaging gateway: nothing is sent, messages are recorded in `message_log`. To send email also set `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM_ADDRESS`, `SMTP_FROM_NAME` |
| `MESSAGING_BACKEND` | unset | `smtp` or `mock`; unset means `smtp` when `SMTP_HOST` is set, else `mock`. SMS is always the mock |
| `CENTRAL_CLIENT_GATEWAY` | `mock` | The central client service. `mock` (the default and so far the only gateway) is an in-memory catalogue of 25 UK clients inside the API process: fine for demonstrations, not a real integration (its state is per process and resets on restart; its `/central-clients/mock/...` endpoints answer 404 in production). See `docs/modules/central-clients.md` |
| `FORWARDED_ALLOW_IPS` | `*` | Lets uvicorn read the client address from `X-Forwarded-For` behind the platform proxy (per-person rate limits) |
| `STORAGE_DIR` | `/var/lib/capium/storage` | Where uploaded files (ticket attachments) are kept. The image already sets this path; override it only to keep the files elsewhere. See [Uploaded files](#uploaded-files) |

All are runtime variables; none is needed at build time. The worker uses the same set.

## Uploaded files

Ticket attachments (Application Testing) are written to the local directory `STORAGE_DIR`, one sub-directory per practice. The
image declares `/var/lib/capium/storage` as a volume and owns it for its unprivileged user (uid 10001, `app`). On a platform:

* **Mount persistent storage at `/var/lib/capium/storage`** on the API container (Coolify: *Persistent Storage*, a named volume or a
  host path). Without one, the files sit in an anonymous volume that a new container does not inherit, so they are lost at every
  deployment. A host path (bind mount) must be writable by uid 10001: `chown -R 10001 <host path>`.
* **Back it up together with the database.** A database restored without its files leaves attachment records whose download
  answers 404 `ATTACHMENT_FILE_MISSING`; files restored without the database are orphans and unreachable.
* Run **one API container** per volume, or put the volume on shared storage: the files are read and written by the API only (the
  worker does not use them).
* The directory is created on first use. If it is missing or read-only, uploads answer 503 `STORAGE_UNAVAILABLE` and nothing else is
  affected. Without Docker, an empty `STORAGE_DIR` means `storage` next to `app/`.
* The reverse proxy must let a 2 MB file through (the UI's nginx accepts bodies up to 16 MB). The API reads an upload under a cap of
  2 MB plus 64 KB and answers 413 `ATTACHMENT_TOO_LARGE` above it.
* `python -m app.maintenance.reset_data` empties the database but **not** this directory: empty it yourself when you start over.
* Deleting a ticket, a comment or an attachment removes its file. Deleting a practice (a soft delete) keeps its files.

## UI environment

| Variable | Example | Notes |
|---|---|---|
| `API_UPSTREAM` | `https://api.timenfee.example.com` | Scheme and host of the API, no path |
| `API_HOST` | `api.timenfee.example.com` | Host header sent to the API (the same host) |
| `VITE_API_URL` | `/api/v1` | Build argument; the default is right for the nginx proxy |

## Releases

1. Run migrations once per release, before the new API starts:
   `alembic upgrade head` (on Coolify: *Pre-deployment command* of the API).
2. Deploy the API and the worker from the same commit, then the UI.
3. A new installation needs its first practice and Owner:
   `python -m app.maintenance.bootstrap --practice-name "..." --admin-name "..." --admin-email ...`
   (prints a temporary password once; the Owner changes it at first sign-in).

## Troubleshooting

| Symptom | Cause |
|---|---|
| `502 Bad Gateway` on the API, no container logs | The container never started: wrong build type (compose instead of Dockerfile), a failed build (see the *deployment* log, not the container log) or the platform routes to a port other than 8000 |
| Container exits at start with a settings error | A required variable is missing or invalid; the message names it (never its value) |
| `/health/ready` answers 503 | Database unreachable, or migrations are behind (run `alembic upgrade head`) |
| UI loads but every API call fails with 502 | `API_UPSTREAM` points at an API that is down or at the wrong host |
| UI shows another product's data | `API_UPSTREAM` points at another product's API |
| Signed out after 15 minutes | The refresh call's `Origin` is not in `CORS_ORIGINS` |
| Everything is slow | The API is far from the database: each request makes several round trips. Run them in the same data centre |
| Uploading a file answers 503 `STORAGE_UNAVAILABLE` | `STORAGE_DIR` is not writable by the API's user (uid 10001): mount the volume and `chown` it, see [Uploaded files](#uploaded-files) |
| Attachments vanished after a deployment | No persistent volume at `/var/lib/capium/storage`: the files were in the old container |
