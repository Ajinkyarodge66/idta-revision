"""Configuration, all from environment variables (see server/DEPLOY.md)."""
import os


def _env(name, default=""):
    return os.environ.get(name, default).strip()


DATABASE_URL = _env("DATABASE_URL", "sqlite:///./idta-dev.db")
ADMIN_KEY = _env("ADMIN_KEY")                 # same key you type into the Admin tab
PEPPER = _env("PEPPER")                       # optional; otherwise kept in the database (migrated from Apps Script)
PACKS_FILE = _env("PACKS_FILE")               # private university packs (packs.json), mounted as a secret; never in git
PACKS_JSON = _env("PACKS_JSON")               # alternative: the JSON itself

# File storage for payment screenshots and exam photos
GCS_BUCKET = _env("GCS_BUCKET")               # production: a private Google Cloud Storage bucket
FILES_DIR = _env("FILES_DIR", "./files")      # local development / tests

# Email: "relay" (send through your existing Apps Script, no new account), "smtp", or "log"
MAIL_MODE = _env("MAIL_MODE", "log")
MAIL_RELAY_URL = _env("MAIL_RELAY_URL")       # your Apps Script /exec URL
SMTP_HOST = _env("SMTP_HOST")
SMTP_PORT = int(_env("SMTP_PORT", "587") or 587)
SMTP_USER = _env("SMTP_USER")
SMTP_PASS = _env("SMTP_PASS")
MAIL_FROM = _env("MAIL_FROM") or SMTP_USER
NOTIFY_EMAIL = _env("NOTIFY_EMAIL")           # where payment / refund / capstone alerts go

MAX_BODY = 4_000_000                          # bytes per request (payment screenshots are the largest)
POOL_SIZE = int(_env("POOL_SIZE", "10") or 10)
TEST_HARNESS = _env("TEST_HARNESS") == "1"    # serves the app pages too, for end-to-end tests only
