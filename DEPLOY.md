# Villages Golf App — Deployment Guide
## Google Cloud Run (Step-by-Step)

Estimated time: **20–30 minutes** (most of it is waiting for builds).

---

## What You'll Need
- A Google Cloud account ✓
- A PIN you'll use to unlock the app on your phone (any 4+ digits)
- A verified sender set up in MailerSend:
  - API token
  - From email address
  - Optional from name

---

## Step 1 — Install Google Cloud CLI

1. Go to: https://cloud.google.com/sdk/docs/install
2. Download and run the installer for Mac
3. Open **Terminal** and run:
   ```
   gcloud init
   ```
4. Sign in with your Google account and select your project.

---

## Step 2 — Enable Required Services

```bash
gcloud services enable run.googleapis.com
gcloud services enable cloudbuild.googleapis.com
```

---

## Step 3 — Navigate to the Project Folder

```bash
cd ~/Documents/villages-golf-app
```

---

## Step 4 — Deploy to Cloud Run

Copy this command, fill in your values, then run it in Terminal:

```bash
gcloud run deploy villages-golf \
  --source . \
  --region us-east1 \
  --allow-unauthenticated \
  --memory 2Gi \
  --timeout 180 \
  --set-env-vars "APP_PIN=YOUR_CHOSEN_PIN" \
  --set-env-vars "MAILERSEND_API_TOKEN=YOUR_MAILERSEND_API_TOKEN" \
  --set-env-vars "MAIL_FROM_EMAIL=YOUR_VERIFIED_FROM_EMAIL" \
  --set-env-vars "MAIL_FROM_NAME=Villages Golf" \
  --set-env-vars "SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
```

Replace:
| Placeholder | Replace with |
|---|---|
| `YOUR_CHOSEN_PIN` | A PIN to unlock the app (e.g. `7291`) |
| `YOUR_MAILERSEND_API_TOKEN` | Your MailerSend API token |
| `YOUR_VERIFIED_FROM_EMAIL` | A sender address verified in MailerSend |

Build takes about 5–8 minutes.

The app no longer stores Villages credentials in Cloud Run environment variables. Each golfer enters their own Villages username, password, and golf PIN inside the app when registering.

### How Environment Variables Work

The `--set-env-vars` lines in the deploy command are where you add your MailerSend settings.

Example:

```bash
--set-env-vars "MAILERSEND_API_TOKEN=YOUR_NEW_TOKEN_HERE"
```

Replace only the value to the right of the `=` sign. Do not add extra quotes inside the value.

### Full Example

```bash
gcloud run deploy villages-golf \
  --source . \
  --region us-east1 \
  --allow-unauthenticated \
  --memory 2Gi \
  --timeout 180 \
  --set-env-vars "APP_PIN=7291" \
  --set-env-vars "MAILERSEND_API_TOKEN=YOUR_NEW_TOKEN_HERE" \
  --set-env-vars "MAIL_FROM_EMAIL=your-verified-sender@example.com" \
  --set-env-vars "MAIL_FROM_NAME=Villages Golf" \
  --set-env-vars "SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
```

Replace:
| Placeholder | Replace with |
|---|---|
| `YOUR_NEW_TOKEN_HERE` | Your MailerSend API token |
| `your-verified-sender@example.com` | A sender address verified in MailerSend |
| `7291` | Your app PIN |

Important:
- Since your previous MailerSend token was exposed, rotate it in MailerSend first and use the new token in this command.
- `MAIL_FROM_EMAIL` must be a verified MailerSend sender address or email sending will fail.

### First Deploy Checklist

1. Verify your sending domain or single sender email in MailerSend.
2. Create a new MailerSend API token with email sending permission.
3. Confirm the exact sender address you will use for `MAIL_FROM_EMAIL`.
4. Run the deploy command above.
5. Open the app after deploy and register a golfer profile.
6. Enter your own email address in the email notifications field.
7. Complete a test booking and confirm the email arrives.
8. If email fails, read the Cloud Run logs:

```bash
gcloud run services logs read villages-golf --region us-east1
```

---

## Step 5 — Get Your App URL

After deploy you'll see:
```
Service URL: https://villages-golf-xxxxxxxx-ue.a.run.app
```

Open it in any browser and bookmark it on your phone.

---

## Step 6 — Add to iPhone Home Screen

1. Open the URL in **Safari** on your iPhone
2. Tap the **Share** button (bottom center)
3. Tap **"Add to Home Screen"**
4. Name it "Villages Golf" → tap **Add**

---

## Updating Mail Settings Later

```bash
gcloud run services update villages-golf \
  --region us-east1 \
  --update-env-vars "MAILERSEND_API_TOKEN=NEW_TOKEN"
```

If you want to update all mail settings at once, use:

```bash
gcloud run services update villages-golf \
  --region us-east1 \
  --update-env-vars "MAILERSEND_API_TOKEN=YOUR_NEW_TOKEN_HERE,MAIL_FROM_EMAIL=your-verified-sender@example.com,MAIL_FROM_NAME=Villages Golf"
```

---

## Cost Estimate

Cloud Run free tier covers ~2–3 bookings/week at **$0/month**.

---

## Troubleshooting

**"Email not sending"** — Check `MAILERSEND_API_TOKEN` and `MAIL_FROM_EMAIL`, and make sure the sender is verified in MailerSend.

**"Login failed"** — Check the Villages username, password, and golf PIN entered during registration.

**"Page timed out"** — The Villages site may be slow; try again in a few minutes.

**View logs:**
```bash
gcloud run services logs read villages-golf --region us-east1
```
