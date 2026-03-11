# Villages Golf App — Deployment Guide
## Google Cloud Run (Step-by-Step)

Estimated time: **20–30 minutes** (most of it is waiting for builds).

---

## What You'll Need
- A Google Cloud account ✓
- Your Villages credentials:
  - TVN username & password (login to thevillages.net)
  - Golf system password (the second password at the golf screen)
- A PIN you'll use to unlock the app on your phone (any 4+ digits)

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

Copy this command, fill in your credentials, then run it in Terminal:

```bash
gcloud run deploy villages-golf \
  --source . \
  --region us-east1 \
  --allow-unauthenticated \
  --memory 2Gi \
  --timeout 180 \
  --set-env-vars "TVN_USERNAME=YOUR_TVN_USERNAME" \
  --set-env-vars "TVN_PASSWORD=YOUR_TVN_PASSWORD" \
  --set-env-vars "GOLF_PASSWORD=YOUR_GOLF_PASSWORD" \
  --set-env-vars "PRIMARY_GOLFER_ID=483204" \
  --set-env-vars "APP_PIN=YOUR_CHOSEN_PIN" \
  --set-env-vars "SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
```

Replace:
| Placeholder | Replace with |
|---|---|
| `YOUR_TVN_USERNAME` | Your thevillages.net username |
| `YOUR_TVN_PASSWORD` | Your thevillages.net password |
| `YOUR_GOLF_PASSWORD` | Your golf system password |
| `YOUR_CHOSEN_PIN` | A PIN to unlock the app (e.g. `7291`) |

Build takes about 5–8 minutes.

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

## Updating Credentials Later

```bash
gcloud run services update villages-golf \
  --region us-east1 \
  --update-env-vars "TVN_PASSWORD=NEW_PASSWORD"
```

---

## Cost Estimate

Cloud Run free tier covers ~2–3 bookings/week at **$0/month**.

---

## Troubleshooting

**"Login failed"** — Check TVN_USERNAME / TVN_PASSWORD, update with command above.

**"Page timed out"** — The Villages site may be slow; try again in a few minutes.

**View logs:**
```bash
gcloud run services logs read villages-golf --region us-east1
```
