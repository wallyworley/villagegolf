# Deploy Runbook — 30-Day Hardening

Branch: `fix/30-day-hardening`. Covers the stability + security cluster from the
2026-07-11 review. **Read the encryption step first — the app will refuse to
boot in production until `USER_DB_ENCRYPTION_KEY` is set.**

## What changed (code)

- **Encryption at rest is now mandatory.** `user_store.py` refuses to run without
  `USER_DB_ENCRYPTION_KEY` (unless `ALLOW_PLAINTEXT_USER_DB=1`, local dev only).
  New `python user_store.py reencrypt` upgrades existing plaintext rows.
- **Booking honesty.** `book_tee_time` no longer reports failure after a slow
  confirmation, nor success without a real reservation number — it reconciles
  against the live reservations list. It also validates the request's
  date/golfers against the session's search context.
- **Worker resilience.** Jobs whose caller already timed out are skipped; a
  wedged worker self-terminates (watchdog) so systemd restarts it; the
  mid-shopping golfer's session is protected from LRU eviction.
- **Web hardening.** IDOR on `/api/remove-user` fixed; `/api/register` rate
  limited; `Secure` cookie + `ProxyFix`; `/healthz`; operator alert emails.
- **Frontend.** Booking has a 120s client timeout, can't be double-tapped, the
  backdrop can't dismiss mid-booking, results clear on any selection change, and
  an unconfirmed booking routes the golfer to "My Tee Times" to verify.

## Deploy order (on the VPS, as root@40.160.233.235)

> Order matters: set the key **before** the new code runs, or boot fails.

```sh
cd /opt/villages-golf-app

# 1. Generate an encryption key (save it somewhere safe too)
KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

# 2. Put secrets in a root-only EnvironmentFile, NOT the world-readable .env
install -m 600 /dev/null /etc/villages-golf.env
{
  echo "USER_DB_ENCRYPTION_KEY=$KEY"
  echo "OPERATOR_ALERT_EMAIL=wallyworley@gmail.com"   # or preferred address
} >> /etc/villages-golf.env
# (SESSION_COOKIE_SECURE defaults on; TRUST_PROXY defaults on — nothing to set.)

# 3. Lock down the existing on-disk secrets the review flagged
chmod 600 .env users.db users.db-wal users.db-shm 2>/dev/null || true

# 4. Point the systemd unit at the EnvironmentFile, then reload
#    Add under [Service]:  EnvironmentFile=/etc/villages-golf.env
#    Recommended for clean watchdog restarts:  Restart=always  RestartSec=2
systemctl edit --full villages-golf.service   # add the line, save
systemctl daemon-reload

# 5. Pull the new code
git fetch origin && git checkout fix/30-day-hardening && git pull

# 6. Encrypt existing rows (key must be exported for this one-shot)
USER_DB_ENCRYPTION_KEY="$KEY" python3 user_store.py reencrypt

# 7. Restart and verify
systemctl restart villages-golf.service
curl -s http://127.0.0.1:8080/healthz   # {"status":"ok"...} once a job has run, else "idle"
journalctl -u villages-golf.service -n 30 --no-pager
```

## Post-deploy manual cleanup

- **Rotate the exposed credentials.** The old plaintext DB + world-readable
  `.env` means TVN passwords/PINs and the `SECRET_KEY`/MailerSend token may be
  considered exposed. Rotating `SECRET_KEY` logs everyone out once (acceptable).
  Ask golfers to re-register, or accept the risk — your call.
- **Remove dead single-user secrets** from the VPS `.env`
  (`TVN_PASSWORD`, `GOLF_PASSWORD`, `APP_PIN`, `PRIMARY_GOLFER_ID`, the GCP/
  Firestore lines) — no longer read by the multi-user code.
- **Uptime monitor** → point a free monitor (e.g. UptimeRobot) at
  `https://villagefairways.com/healthz`; alert on non-200.

## Still TODO (not in this branch)

- **Selector-drift canary**: a systemd timer that logs in through glf109c on a
  test account nightly and alerts on failure — so drift is caught at 6 AM, not
  7:00:15. Needs a dedicated test account; deferred.

## Rollback

`git checkout master && systemctl restart villages-golf.service`. Note: rows
encrypted in step 6 stay encrypted; the old code on `master` also reads them
**only if** `USER_DB_ENCRYPTION_KEY` is still set in the environment. Keep the
key set even on rollback.
