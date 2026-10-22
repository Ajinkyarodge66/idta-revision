# Python backend: deploy guide

This replaces the Google Apps Script backend with a Python server (FastAPI) and a Postgres database.
Students notice nothing except that it's faster. Their accounts, passwords, progress, purchases and certificates all move across.

## Why

| | Apps Script + Sheets (old) | Python + Postgres (new) |
|---|---|---|
| Requests at the same moment | about 30 for the whole site | about 80 per server, and Google adds servers automatically (up to the limit you set) |
| Every request | reads the whole Users sheet | one indexed database lookup |
| Locking | one global lock; everyone waits | row locks; students never wait for each other |
| Files | Google Drive | private Cloud Storage bucket |

## What it costs

- **Google Cloud Run**: the free tier is about 2 million requests a month. After that, roughly $0.40 per million requests plus CPU time.
- **Neon Postgres**: free for 0.5 GB, which is enough for thousands of students. The Launch plan is about $19/month if you outgrow it.
- **Cloud Storage** for screenshots and exam photos: a few pence a month.
- Optional: `--min-instances 1` removes the 1–2 second cold start after a quiet period. It costs about $6–10/month.

## Steps

You do steps 1–2, because they need your accounts and a card. Claude can type steps 3–6 in Cloud Shell for you, apart from pasting the two secrets.

1. **Database.** Sign up at neon.tech with Google. Create a project in region *AWS Europe West 2 (London)*. Copy the **pooled** connection string (it starts `postgresql://` and contains `-pooler`).
2. **Google Cloud.** At console.cloud.google.com, create a project (for example `idta-backend`) and turn on billing. You get the free tier above.
3. **Open Cloud Shell** (the `>_` icon at the top right of the console) and run:

```bash
PROJECT=$(gcloud config get project); REGION=europe-west2
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com storage.googleapis.com
gcloud storage buckets create gs://$PROJECT-idta-files --location=$REGION --uniform-bucket-level-access --public-access-prevention
git clone https://github.com/ajinkyarodge66/idta-revision && cd idta-revision/server
```

4. **Secrets.** Paste these yourself. Nothing shows on screen while you paste.

```bash
read -s -p "Admin key: " K; echo; printf %s "$K" | gcloud secrets create admin-key --data-file=-; unset K
read -s -p "Neon pooled connection string: " D; echo; printf %s "$D" | gcloud secrets create db-url --data-file=-; unset D
SA=$(gcloud projects describe $PROJECT --format='value(projectNumber)')-compute@developer.gserviceaccount.com
for s in admin-key db-url; do gcloud secrets add-iam-policy-binding $s --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor -q >/dev/null; done
gcloud storage buckets add-iam-policy-binding gs://$PROJECT-idta-files --member=serviceAccount:$SA --role=roles/storage.objectAdmin -q >/dev/null
```

5. **Deploy.** Use your current Apps Script `/exec` URL as `RELAY` (emails keep going out from your Gmail through it).

```bash
RELAY="https://script.google.com/macros/s/AKfycby-OwGobl9mg0MgXzQ5Y5llc4vTx3Az62l1Yq6ET7ilR4AjypOTRb9mY6RRlBHHTaOp/exec"
gcloud run deploy idta-api --source . --region $REGION --allow-unauthenticated \
  --set-env-vars GCS_BUCKET=$PROJECT-idta-files,MAIL_MODE=relay,MAIL_RELAY_URL=$RELAY,POOL_SIZE=5 \
  --set-secrets ADMIN_KEY=admin-key:latest,DATABASE_URL=db-url:latest \
  --concurrency 80 --max-instances 20 --cpu 1 --memory 512Mi --timeout 60
```

It prints a **Service URL** such as `https://idta-api-xxxx.a.run.app`. Open `<URL>/healthz` and check that it shows `{"ok":true}`.

6. **Copy the data across.** Deploy the updated `backend/Code.gs` first, because it adds the export and email-relay actions. Then:

```bash
gcloud run jobs deploy idta-migrate --source . --region $REGION --set-env-vars GCS_BUCKET=$PROJECT-idta-files \
  --set-secrets ADMIN_KEY=admin-key:latest,DATABASE_URL=db-url:latest --command python --args migrate.py,--from,$RELAY,--wipe
gcloud run jobs execute idta-migrate --region $REGION --wait
```

7. **Switch the websites.** Rebuild both sites with the new URL (`node build.js "https://idta-api-xxxx.a.run.app"`) and push them. Run step 6 once more just before you push, so nothing written in the meantime is lost.

The old Apps Script stays in place. It sends the emails, and if anything goes wrong you can switch back by rebuilding with its URL.

## Updating the server later

```bash
cd ~/idta-revision && git pull -q && bash server/clouddeploy.sh
```

## Built for scale

- **Running totals.** The leaderboard, admin overview and analytics read small summary tables (`user_stats`, `topic_stats`,
  `question_stats`, `day_activity`), which `app/rollup.py` keeps up to date in the background. They don't scan every answer ever given.
- **Bounded responses.** The leaderboard returns the top 300, plus your own row. The admin page lists the 1,000 most recently
  active students and searches the rest on the server. The export downloads everyone page by page.
- **Automatic scaling.** Cloud Run runs up to 100 server copies of 80 requests each, and Neon's connection pooler shares database connections.
- **Safety limits.** There's a 25-second limit per query, a flood guard per IP (generous enough for a whole campus on one network),
  4 MB request size, gzip responses, and retries on brief database hiccups.
- **What to upgrade as you grow:** Neon storage and compute past roughly 10,000 active students, and email through Brevo or Resend
  (`MAIL_MODE=smtp`) past 100 emails a day.

## Running it yourself

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080        # SQLite by default; set DATABASE_URL for Postgres
pytest -q                                         # API tests
python loadtest.py http://localhost:8080/ 300 60  # 300 simultaneous students for a minute
```

## Settings (environment variables)

| Name | What it is |
|---|---|
| `DATABASE_URL` | Postgres connection string (secret) |
| `ADMIN_KEY` | the key you type into the Admin tab (secret) |
| `GCS_BUCKET` | private bucket for screenshots and exam photos |
| `MAIL_MODE` | `relay` (through Apps Script), `smtp`, or `log` |
| `MAIL_RELAY_URL` | Apps Script `/exec` URL when `MAIL_MODE=relay` |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `MAIL_FROM` | when `MAIL_MODE=smtp` (e.g. Brevo or Resend; above 100 emails a day) |
| `NOTIFY_EMAIL` | where payment, refund and capstone alerts go (otherwise the value copied from Apps Script) |
| `POOL_SIZE` | database connections per server process (keep it at 5 on Neon's free tier) |

Private university packs are copied into the database by `migrate.py`, so they never sit in GitHub.
