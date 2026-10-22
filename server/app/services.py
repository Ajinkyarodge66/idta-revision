"""Email, file storage, caches and rate limits."""
import json
import logging
import os
import smtplib
import threading
import time
import urllib.request
from collections import OrderedDict
from email.message import EmailMessage

from sqlalchemy import case, delete, insert, select, update

from . import settings
from .db import counters
from .util import now_ms

log = logging.getLogger("idta")


# ---------- email ----------
class Mailer:
    def __init__(self):
        self.sent = []          # only used by the test harness

    def send(self, to, subject, body):
        if not to:
            return
        mode = settings.MAIL_MODE
        try:
            if mode == "test":
                self.sent.append({"to": to, "subject": subject, "body": body})
            elif mode == "relay":
                data = json.dumps({"action": "relayMail", "key": settings.ADMIN_KEY, "to": to, "subject": subject, "body": body}).encode()
                req = urllib.request.Request(settings.MAIL_RELAY_URL, data=data, headers={"Content-Type": "text/plain"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    res = json.loads(r.read().decode() or "{}")
                    if not res.get("ok"):
                        log.warning("mail relay refused: %s", res)
            elif mode == "smtp":
                msg = EmailMessage()
                msg["From"], msg["To"], msg["Subject"] = settings.MAIL_FROM, to, subject
                msg.set_content(body)
                with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as s:
                    s.starttls()
                    if settings.SMTP_USER:
                        s.login(settings.SMTP_USER, settings.SMTP_PASS)
                    s.send_message(msg)
            else:
                log.info("EMAIL to=%s subject=%s", to, subject)
        except Exception as e:      # an email failure must never break the request
            log.warning("email to %s failed: %s", to, e)


mailer = Mailer()


# ---------- files ----------
class LocalStore:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    def _p(self, key):
        p = os.path.abspath(os.path.join(self.root, key))
        if not p.startswith(self.root + os.sep):
            raise ValueError("bad key")
        return p

    def put(self, key, data, mime):
        p = self._p(key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        with open(p + ".mime", "w") as f:
            f.write(mime)

    def get(self, key):
        p = self._p(key)
        try:
            with open(p, "rb") as f:
                data = f.read()
            with open(p + ".mime") as f:
                mime = f.read()
            return data, mime
        except FileNotFoundError:
            return None, None

    def delete(self, key):
        for p in (self._p(key), self._p(key) + ".mime"):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass

    def count(self, prefix):
        d = self._p(prefix.rstrip("/") + "/x")
        d = os.path.dirname(d)
        return len([f for f in os.listdir(d) if not f.endswith(".mime")]) if os.path.isdir(d) else 0


class GCSStore:
    def __init__(self, bucket):
        from google.cloud import storage   # imported only in production
        self.b = storage.Client().bucket(bucket)

    def put(self, key, data, mime):
        self.b.blob(key).upload_from_string(data, content_type=mime)

    def get(self, key):
        bl = self.b.blob(key)
        try:
            data = bl.download_as_bytes()
        except Exception:
            return None, None
        return data, bl.content_type or "image/jpeg"

    def delete(self, key):
        try:
            self.b.blob(key).delete()
        except Exception:
            pass


_store = None


def store():
    global _store
    if _store is None:
        _store = GCSStore(settings.GCS_BUCKET) if settings.GCS_BUCKET else LocalStore(settings.FILES_DIR)
    return _store


# ---------- in-process caches ----------
class TTLCache:
    def __init__(self, maxsize=50000):
        self.d = OrderedDict()
        self.max = maxsize
        self.lock = threading.Lock()

    def get(self, k):
        with self.lock:
            v = self.d.get(k)
            if not v:
                return None
            if v[1] < time.time():
                self.d.pop(k, None)
                return None
            self.d.move_to_end(k)
            return v[0]

    def put(self, k, val, secs):
        with self.lock:
            self.d[k] = (val, time.time() + secs)
            self.d.move_to_end(k)
            while len(self.d) > self.max:
                self.d.popitem(last=False)

    def remove(self, k):
        with self.lock:
            self.d.pop(k, None)

    def clear(self):
        with self.lock:
            self.d.clear()


cache = TTLCache()


# ---------- rate limits (stored in the database so every instance shares them) ----------
def too_many(c, key, limit):
    r = c.execute(select(counters.c.n, counters.c.exp).where(counters.c.key == key)).first()
    return bool(r and r.exp > now_ms() and r.n >= limit)


def bump(c, key, secs):
    """Add one to a counter atomically; the window restarts at each bump (like the old CacheService)."""
    now = now_ms()
    exp = now + secs * 1000
    upd = update(counters).where(counters.c.key == key).values(
        n=case((counters.c.exp > now, counters.c.n + 1), else_=1), exp=exp)
    if c.execute(upd).rowcount:
        return
    try:
        with c.begin_nested():
            c.execute(insert(counters).values(key=key, n=1, exp=exp))
    except Exception:
        c.execute(upd)


class LocalCounter:
    """Per-instance counter for very hot keys (visit flood guard) where a shared row would be a bottleneck."""
    def __init__(self):
        self.lock = threading.Lock()
        self.n, self.reset = 0, 0

    def hit(self, limit, secs):
        with self.lock:
            t = time.time()
            if t > self.reset:
                self.n, self.reset = 0, t + secs
            self.n += 1
            return self.n > limit


def forget(c, key):
    c.execute(delete(counters).where(counters.c.key == key))
