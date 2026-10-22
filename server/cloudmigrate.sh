#!/bin/bash
# Copy all data from the Apps Script backend into the new database. Safe to run again (it empties the new tables first).
set -e
cd "$(dirname "$0")"
PROJECT=$(gcloud config get project 2>/dev/null); REGION=europe-west2
RELAY="https://script.google.com/macros/s/AKfycby-OwGobl9mg0MgXzQ5Y5llc4vTx3Az62l1Yq6ET7ilR4AjypOTRb9mY6RRlBHHTaOp/exec"
gcloud run jobs deploy idta-migrate --source . --region $REGION -q --set-env-vars GCS_BUCKET=$PROJECT-idta-files \
  --set-secrets ADMIN_KEY=admin-key:latest,DATABASE_URL=db-url:latest --command python --args "migrate.py,--from,$RELAY,--wipe" --task-timeout 1800
gcloud run jobs execute idta-migrate --region $REGION --wait
gcloud logging read "resource.type=cloud_run_job AND resource.labels.job_name=idta-migrate" --limit 5 --format='value(textPayload)' --freshness=15m | grep -E "Copied|Done|failed|Error" | head
echo "MIGRATION DONE"
