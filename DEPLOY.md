# Villages Golf App: VPS deployment guide

The app runs on a self-hosted VPS. gunicorn serves it, Caddy terminates TLS and reverse-proxies to it, and systemd keeps it running. There is no Google Cloud dependency: user profiles live in a local SQLite file. Live at https://villagefairways.com.

## Server layout

- Host: `root@40.160.233.235` (SSH via key).
- App directory: `/opt/villages-golf-app` (a git checkout of `master`).
- Process: gunicorn (1 worker, 4 threads, `--timeout 300`) bound to `127.0.0.1:8080`, managed by the systemd unit `villages-golf.service`. This timeout value lives in the systemd unit file, so edit it there, not in this guide.
- TLS and reverse proxy: Caddy on 443 auto-provisions a Let's Encrypt certificate and proxies to `127.0.0.1:8080`. Do not install nginx (it would conflict on ports 80/443).
- User data: SQLite at `/opt/villages-golf-app/users.db`, encrypted at rest when `USER_DB_ENCRYPTION_KEY` is set.
- Config: `/opt/villages-golf-app/.env` (chmod 600). systemd loads it via `EnvironmentFile`, so it is re-read only on restart.

## One-time prerequisites

- A stable `SECRET_KEY`. Generate it once and reuse it forever:
  ```bash
  openssl rand -hex 32
  ```
  Changing it later logs everyone out (Flask signs session cookies with it).
- A verified MailerSend sender: an API token, a from-address on a verified sending domain, and an optional from name. Do not use a MailerSend trial domain (`*.mlsender.net`) as the from-address: it only delivers to the account admin's own inbox.

## Before you deploy

Back up the database and record the current commit, so you can roll back if the deploy goes wrong.

1. Back up the database:
   ```bash
   ssh root@40.160.233.235
   cd /opt/villages-golf-app
   cp users.db "users.db.bak-$(date +%Y%m%d-%H%M%S)"
   ```
2. Record the current commit, and save the value somewhere you can find it later:
   ```bash
   git rev-parse --short HEAD
   ```

## Deploy a code update

Push to `master`, then pull, reinstall dependencies, and restart on the VPS:

```bash
# on your machine
git push origin master

# on the VPS
ssh root@40.160.233.235
cd /opt/villages-golf-app
git pull --ff-only origin master
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
systemctl restart villages-golf.service
systemctl is-active villages-golf.service   # expect: active
curl -fsS http://127.0.0.1:8080/healthz
```

Playwright 1.62.0 needs its matching Chromium build, so run `playwright install chromium` after every pull, even when `requirements.txt` did not change the Playwright version. The final `curl` confirms the app answers before you consider the deploy finished.

## Roll back a release

Check out the previous commit, reinstall dependencies, and restart:

```bash
ssh root@40.160.233.235
cd /opt/villages-golf-app
git checkout <previous-commit>
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
systemctl restart villages-golf.service
systemctl is-active villages-golf.service   # expect: active
curl -fsS http://127.0.0.1:8080/healthz
```

Use the commit you recorded in "Before you deploy" as `<previous-commit>`. The checkout leaves the repository on a detached commit, and that is the state to stay in until a fix is ready. Do not run a bare `git checkout master` after rolling back: the local `master` branch still points at the release you rolled back from, so checking it out puts that code back on disk, and the next restart for any reason would serve it again. When a fix is on `master`, deploy it with the "Deploy a code update" steps, starting with `git checkout master` immediately followed by `git pull --ff-only origin master`, the reinstall, the restart, and the health check, so the repository never sits with the broken code on disk. Restore the database backup only if the release you are rolling back from changed the database schema. This release adds columns additively, so it does not need a restore.

## Change environment variables (including mail settings)

All config lives in `/opt/villages-golf-app/.env`. Edit the file, then restart so systemd re-reads it:

```bash
ssh root@40.160.233.235
cd /opt/villages-golf-app
nano .env          # e.g. MAILERSEND_API_TOKEN, MAIL_FROM_EMAIL, MAIL_FROM_NAME
systemctl restart villages-golf.service
```

Common vars: `SECRET_KEY`, `MAILERSEND_API_TOKEN`, `MAIL_FROM_EMAIL`, `MAIL_FROM_NAME`, `USER_DB_ENCRYPTION_KEY`, `LOG_LEVEL`. Do not set the local-dev-only flags `SESSION_COOKIE_SECURE=0` or `ALLOW_PLAINTEXT_USER_DB=1` on the VPS.

## View logs

```bash
journalctl -u villages-golf.service -n 100 --no-pager   # recent lines
journalctl -u villages-golf.service -f                  # follow live
```

Email results appear here: `email.sent ... status=202` on success, and `email.auth_fail ... status=401` when the MailerSend token is invalid or the sender is not verified.

## Add to the iPhone Home Screen

1. Open https://villagefairways.com in Safari.
2. Tap Share, then "Add to Home Screen."
3. Name it "Villages Golf" and tap Add.

## Docker alternative

You can also run the app from a Docker image instead of a git checkout with a venv and a systemd unit. The image builds from the base image `mcr.microsoft.com/playwright/python:v1.62.0-noble` and runs as the non-root user `pwuser`. Mount a `/data` volume for persistent storage, and set `USER_DB_PATH=/data/users.db` so the encrypted user store survives a container restart.

## Troubleshooting

- Email not sending: check `journalctl` for `email.auth_fail`. Confirm `MAILERSEND_API_TOKEN` is valid and `MAIL_FROM_EMAIL` sits on a verified MailerSend domain.
- Login failed: the Villages username, password, or golf PIN entered at registration is wrong.
- Page timed out: the Villages site may be slow. Try again in a few minutes.
- Service will not start: run `systemctl status villages-golf.service` and `journalctl -u villages-golf.service -n 50`.

Deeper server setup notes (initial provisioning, Caddy config, Ubuntu 24.04 Playwright libraries) live on the VPS at `/opt/villages-golf-app/DEPLOY_VPS_NOTES.md`.
