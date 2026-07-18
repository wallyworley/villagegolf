# Villages Golf App: VPS Deployment Guide

The app runs on a self-hosted VPS. gunicorn serves it, Caddy terminates TLS and reverse-proxies to it, and systemd keeps it running. There is no Google Cloud dependency: user profiles live in a local SQLite file. Live at https://villagefairways.com.

## Server Layout

- Host: `root@40.160.233.235` (SSH via key).
- App directory: `/opt/villages-golf-app` (a git checkout of `master`).
- Process: gunicorn (1 worker, 4 threads, `--timeout 250`) bound to `127.0.0.1:8080`, managed by the systemd unit `villages-golf.service`.
- TLS and reverse proxy: Caddy on 443 auto-provisions a Let's Encrypt certificate and proxies to `127.0.0.1:8080`. Do not install nginx (it would conflict on ports 80/443).
- User data: SQLite at `/opt/villages-golf-app/users.db`, encrypted at rest when `USER_DB_ENCRYPTION_KEY` is set.
- Config: `/opt/villages-golf-app/.env` (chmod 600). systemd loads it via `EnvironmentFile`, so it is re-read only on restart.

## One-Time Prerequisites

- A stable `SECRET_KEY`. Generate it once and reuse it forever:
  ```bash
  openssl rand -hex 32
  ```
  Changing it later logs everyone out (Flask signs session cookies with it).
- A verified MailerSend sender: an API token, a from-address on a verified sending domain, and an optional from name. Do not use a MailerSend trial domain (`*.mlsender.net`) as the from-address: it only delivers to the account admin's own inbox.

## Deploy a Code Update

Push to `master`, then pull and restart on the VPS:

```bash
# on your machine
git push origin master

# on the VPS
ssh root@40.160.233.235
cd /opt/villages-golf-app
git pull --ff-only origin master
systemctl restart villages-golf.service
systemctl is-active villages-golf.service   # expect: active
```

## Change Environment Variables (Including Mail Settings)

All config lives in `/opt/villages-golf-app/.env`. Edit the file, then restart so systemd re-reads it:

```bash
ssh root@40.160.233.235
cd /opt/villages-golf-app
nano .env          # e.g. MAILERSEND_API_TOKEN, MAIL_FROM_EMAIL, MAIL_FROM_NAME
systemctl restart villages-golf.service
```

Common vars: `SECRET_KEY`, `MAILERSEND_API_TOKEN`, `MAIL_FROM_EMAIL`, `MAIL_FROM_NAME`, `USER_DB_ENCRYPTION_KEY`, `LOG_LEVEL`. Do not set the local-dev-only flags `SESSION_COOKIE_SECURE=0` or `ALLOW_PLAINTEXT_USER_DB=1` on the VPS.

## View Logs

```bash
journalctl -u villages-golf.service -n 100 --no-pager   # recent lines
journalctl -u villages-golf.service -f                  # follow live
```

Email results appear here: `email.sent ... status=202` on success, and `email.auth_fail ... status=401` when the MailerSend token is invalid or the sender is not verified.

## Add to iPhone Home Screen

1. Open https://villagefairways.com in Safari.
2. Tap Share, then "Add to Home Screen".
3. Name it "Villages Golf" and tap Add.

## Troubleshooting

- Email not sending: check `journalctl` for `email.auth_fail`. Confirm `MAILERSEND_API_TOKEN` is valid and `MAIL_FROM_EMAIL` sits on a verified MailerSend domain.
- Login failed: the Villages username, password, or golf PIN entered at registration is wrong.
- Page timed out: the Villages site may be slow. Try again in a few minutes.
- Service will not start: run `systemctl status villages-golf.service` and `journalctl -u villages-golf.service -n 50`.

Deeper server setup notes (initial provisioning, Caddy config, Ubuntu 24.04 Playwright libraries) live on the VPS at `/opt/villages-golf-app/DEPLOY_VPS_NOTES.md`.
