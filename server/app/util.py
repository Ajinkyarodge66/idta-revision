"""Small helpers that mirror the validation rules of the original Apps Script backend."""
import datetime as _dt
import hashlib
import math
import re
import time
import uuid

CTRL = re.compile(r"[\u0000-\u001f]")


def now_ms():
    return int(time.time() * 1000)


def iso(ms=None):
    """Same format as JavaScript's toISOString()."""
    d = _dt.datetime.fromtimestamp((now_ms() if ms is None else ms) / 1000, _dt.timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def parse_ms(s):
    """ISO string (or ms number) to epoch ms; None if it can't be read."""
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)) and not isinstance(s, bool):
        return int(s)
    try:
        t = str(s).strip().replace("Z", "+00:00")
        d = _dt.datetime.fromisoformat(t)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return int(d.timestamp() * 1000)
    except ValueError:
        return None


def day_key(ms):
    return _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc).strftime("%Y-%m-%d")


def js_str(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (list, dict)):
        return ""
    return str(v)


def js_num(v):
    """JavaScript Number(v), returning None for NaN."""
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    s = str(v).strip()
    if s == "":
        return 0.0
    try:
        n = float(s)
        return n if math.isfinite(n) else None
    except ValueError:
        return None


def num_or_none(v):
    """num_() in the old backend: blank for missing or non-numbers."""
    if v is None or v == "":
        return None
    n = js_num(v)
    return n


def text(v, mx=60):
    return CTRL.sub(" ", js_str(v)).strip()[:mx]


plain = text


def multiline(v, mx):
    return "\n".join(text(l, 400) for l in re.split(r"\r?\n", js_str(v))).strip()[:mx]


def mobile(v):
    d = re.sub(r"\D", "", js_str(v))
    return d if 8 <= len(d) <= 15 else ""


def email(v):
    s = js_str(v).strip().lower()
    return s if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]{2,}", s) and len(s) <= 100 else ""


def pw(v):
    s = js_str(v)
    return s if re.fullmatch(r"[a-f0-9]{64}", s) else ""


def sha(s):
    return hashlib.sha256(js_str(s).encode("utf-8")).hexdigest()


def pid(uid):
    return sha("pid|" + js_str(uid))[:12]


def ref_code(uid):
    return pid(uid)[:8].upper()


def uid_new():
    return str(uuid.uuid4())


def short_id(prefix, n):
    return prefix + sha(uuid.uuid4().hex)[:n].upper()


def country(v):
    c = js_str(v).upper()
    return c if re.fullmatch(r"[A-Z]{2}", c) else ""


def site(v):
    return "global" if v == "global" else "uk"


def src(v):
    return re.sub(r"[^a-z0-9._-]", "", js_str(v).lower())[:40]


def dev(v):
    return js_str(v) if js_str(v) in ("phone", "tablet", "desktop") else "unknown"


def money(v):
    n = js_num(v)
    if n is None:
        return 0
    n = round(n * 100) / 100
    return min(n, 1000) if n > 0 else 0


def coupon_code(v):
    s = re.sub(r"[^A-Z0-9-]", "", js_str(v).upper())
    return s if 4 <= len(s) <= 24 else ""


def ref_norm(v):
    """Payment reference reduced to letters and digits, for duplicate checks."""
    return re.sub(r"[^A-Z0-9]", "", js_str(v).upper())[:40]


def b64ok(s):
    return bool(re.fullmatch(r"[A-Za-z0-9+/=]+", s))


def out_num(v):
    """Numbers as JavaScript would print them in JSON (3.0 -> 3), blanks as ''."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v
