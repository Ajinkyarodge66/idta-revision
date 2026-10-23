#!/bin/bash
# Redeploy the backend with the latest code (no questions asked; uses the secrets already saved).
#   cd ~/idta-revision && git pull -q && bash server/clouddeploy.sh
set -e
cd "$(dirname "$0")"
PROJECT=$(gcloud config get project 2>/dev/null); REGION=europe-west2; BUCKET=$PROJECT-idta-files
RELAY="https://script.google.com/macros/s/AKfycby-OwGobl9mg0MgXzQ5Y5llc4vTx3Az62l1Yq6ET7ilR4AjypOTRb9mY6RRlBHHTaOp/exec"
gcloud run deploy idta-api --source . --region $REGION --allow-unauthenticated -q \
  --set-env-vars GCS_BUCKET=$BUCKET,MAIL_MODE=relay,MAIL_RELAY_URL=$RELAY,POOL_SIZE=5 \
  --set-secrets ADMIN_KEY=admin-key:latest,DATABASE_URL=db-url:latest \
  --concurrency 80 --max-instances ${MAX_INSTANCES:-20} --cpu 1 --memory 512Mi --timeout 60 --cpu-boost
URL=$(gcloud run services describe idta-api --region $REGION --format='value(status.url)')
curl -s "$URL/health"; echo
echo "DEPLOYED: $URL"
