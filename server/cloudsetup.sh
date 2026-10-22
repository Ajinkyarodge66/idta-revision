#!/bin/bash
# One-time Google Cloud setup for the Python backend. Run in Cloud Shell:  bash idta-revision/server/cloudsetup.sh
set -e
cd "$(dirname "$0")"
PROJECT=$(gcloud config get project 2>/dev/null); REGION=europe-west2; BUCKET=$PROJECT-idta-files
RELAY="https://script.google.com/macros/s/AKfycby-OwGobl9mg0MgXzQ5Y5llc4vTx3Az62l1Yq6ET7ilR4AjypOTRb9mY6RRlBHHTaOp/exec"
echo "== Project: $PROJECT"
echo "== 1/5 Turning on Cloud Run, Build, Secret Manager and Storage (about a minute)"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com storage.googleapis.com
echo "== 2/5 Private bucket for screenshots and exam photos"
gcloud storage buckets describe gs://$BUCKET >/dev/null 2>&1 || gcloud storage buckets create gs://$BUCKET --location=$REGION --uniform-bucket-level-access --public-access-prevention
echo "== 3/5 Secrets (what you paste is hidden and never shown)"
secret() {  # name, prompt
  if gcloud secrets describe "$1" >/dev/null 2>&1; then
    read -r -p "   $1 already saved. Replace it? [y/N] " a; [ "$a" = "y" ] || return 0
    read -r -s -p "   $2: " V; echo; printf %s "$V" | gcloud secrets versions add "$1" --data-file=- >/dev/null
  else
    read -r -s -p "   $2: " V; echo; printf %s "$V" | gcloud secrets create "$1" --data-file=- >/dev/null
  fi
  unset V; echo "   saved $1"
}
secret db-url "Paste the Neon connection string (postgresql://...), then press Enter"
secret admin-key "Paste your admin key, then press Enter"
SA=$(gcloud projects describe $PROJECT --format='value(projectNumber)')-compute@developer.gserviceaccount.com
for s in admin-key db-url; do gcloud secrets add-iam-policy-binding $s --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor -q >/dev/null; done
gcloud storage buckets add-iam-policy-binding gs://$BUCKET --member=serviceAccount:$SA --role=roles/storage.objectAdmin -q >/dev/null
gcloud projects add-iam-policy-binding $PROJECT --member=serviceAccount:$SA --role=roles/cloudbuild.builds.builder -q >/dev/null 2>&1 || true
echo "== 4/5 Building and deploying the server (3-6 minutes)"
gcloud run deploy idta-api --source . --region $REGION --allow-unauthenticated -q \
  --set-env-vars GCS_BUCKET=$BUCKET,MAIL_MODE=relay,MAIL_RELAY_URL=$RELAY,POOL_SIZE=5 \
  --set-secrets ADMIN_KEY=admin-key:latest,DATABASE_URL=db-url:latest \
  --concurrency 80 --max-instances 20 --cpu 1 --memory 512Mi --timeout 60
URL=$(gcloud run services describe idta-api --region $REGION --format='value(status.url)')
echo "== 5/5 Health check"
curl -s "$URL/healthz"; echo
curl -s -X POST "$URL/" -d '{"action":"config"}' | head -c 200; echo
echo
echo "BACKEND URL: $URL"
echo "ALL DONE"
