"""Every backend action. Requests and responses match the original Google Apps Script backend exactly,
so the website only needs its SYNC_URL changed. All requests: POST {action, ...}."""
import base64
import hashlib
import hmac
import io
import json
import math
import re
import secrets

from sqlalchemy import and_, case, delete, distinct, func, insert, or_, select, true, update
from sqlalchemy.exc import IntegrityError

from . import settings
from .db import (capstones, certs, coupons, day_activity, events, news, payments, payouts, proctor, question_stats, questions, refunds,
                 sessions, settings_t, topic_stats, user_stats, users, visits)
from .services import LocalCounter, bump, cache, forget, store, too_many
from .util import (b64ok, country, coupon_code, day_key, dev, email, iso, js_num, js_str, mobile, money,
                   multiline, now_ms, num_or_none, out_num, parse_ms, pid, plain, pw, ref_code, ref_norm, sha,
                   short_id, site, src, text, uid_new)

APP = "IDTA Revision"
BRANDS = {"uk": "IDTA Revision", "global": "JobReady AI"}
PHOTO_DAYS = 30
REF_REWARD, REF_DAYS = 1, 10
SESSION_DAYS = 30
MAX_PROGRESS_CHARS = 45000
DAY = 86_400_000
TOPIC_KEYS = ["data", "dist", "prep", "cls", "eval", "nn", "ens", "reg", "arm", "clu", "py", "math", "nlp", "genai", "ops",
              "stats", "dl", "llm", "prod"]
DEFAULT_DOMAINS = {"myport.ac.uk": "University of Portsmouth", "port.ac.uk": "University of Portsmouth"}
DEFAULT_REGIONS = [
    {"id": "GB", "name": "United Kingdom", "countries": ["GB"], "cur": "GBP", "amount": 3, "label": "£3"},
    {"id": "IN", "name": "India", "countries": ["IN"], "cur": "INR", "amount": 199, "label": "₹199"},
    {"id": "PK", "name": "Pakistan", "countries": ["PK"], "cur": "PKR", "amount": 499, "label": "Rs 499"},
    {"id": "BD", "name": "Bangladesh", "countries": ["BD"], "cur": "BDT", "amount": 249, "label": "৳249"},
    {"id": "NG", "name": "Nigeria", "countries": ["NG"], "cur": "NGN", "amount": 1500, "label": "₦1,500"},
    {"id": "AE", "name": "UAE", "countries": ["AE"], "cur": "AED", "amount": 12, "label": "AED 12"},
    {"id": "NA", "name": "USA & Canada", "countries": ["US", "CA"], "cur": "USD", "amount": 3.99, "label": "$3.99"},
    {"id": "EU", "name": "Europe", "countries": ["IE", "DE", "FR", "ES", "IT", "NL", "BE", "PT", "AT", "FI", "GR", "PL", "SE", "DK", "CZ",
                                              "RO", "HU", "SK", "SI", "HR", "LT", "LV", "EE", "LU", "MT", "CY", "BG"],
     "cur": "EUR", "amount": 3.49, "label": "€3.49"},
    {"id": "OTHER", "name": "Rest of the world", "countries": [], "cur": "USD", "amount": 2.99, "label": "$2.99"},
]
TO_GBP = {"GBP": 1, "INR": 0.0091, "PKR": 0.0027, "BDT": 0.0066, "NGN": 0.0005, "AED": 0.21, "USD": 0.77, "EUR": 0.86, "CAD": 0.56,
          "LKR": 0.0026, "NPR": 0.0057, "KES": 0.006, "GHS": 0.05, "MYR": 0.17, "SAR": 0.2, "AUD": 0.5}
ACTIVITY = ("q", "mock", "lab")
MONEY_EVENTS = ("unlock", "revoke", "refund", "payout", "restore")
hits_guard = LocalCounter()


class Ctx:
    """One request: a database transaction, the caller's IP, and emails to send once it has committed."""
    def __init__(self, conn, ip=""):
        self.c, self.ip, self.mails = conn, ip, []

    def mail(self, to, subject, body):
        self.mails.append((js_str(to), subject, body))

    def notify(self, subject, body):
        to = settings.NOTIFY_EMAIL or get_setting(self.c, "NOTIFY_EMAIL")
        if to:
            self.mail(to, APP + ": " + subject, body)


def fail(error, **extra):
    return {"ok": False, "error": error, **extra}


API = {}


def action(fn):
    API[fn.__name__.rstrip("_")] = fn
    return fn


def brand(s):
    return BRANDS[site(s)]


# ---------- settings (were Apps Script "script properties") ----------
def get_setting(c, key, default=None):
    r = c.execute(select(settings_t.c.value).where(settings_t.c.key == key)).first()
    return r.value if r else default


def set_setting(c, key, value):
    if c.execute(update(settings_t).where(settings_t.c.key == key).values(value=value)).rowcount == 0:
        c.execute(insert(settings_t).values(key=key, value=value))


def json_setting(c, key, default):
    try:
        v = json.loads(get_setting(c, key) or "null")
    except ValueError:
        v = None
    return default if v is None else v


_pepper = None


def pepper(c):
    global _pepper
    if settings.PEPPER:
        return settings.PEPPER
    if _pepper:
        return _pepper
    v = get_setting(c, "PEPPER")
    if not v:
        v = uid_new() + uid_new()
        try:
            with c.begin_nested():
                c.execute(insert(settings_t).values(key="PEPPER", value=v))
        except IntegrityError:
            v = get_setting(c, "PEPPER")
    _pepper = v
    return v


def hash_pw(c, derived, salt):
    """The browser already ran PBKDF2 (150k rounds); this adds a per-user salt and a server-only pepper."""
    h = salt + "|" + derived + "|" + pepper(c)
    for _ in range(50):
        h = hashlib.sha256(h.encode()).hexdigest()
    return h


def pw_matches(c, derived, row):
    return bool(derived) and hmac.compare_digest(hash_pw(c, derived, row["salt"]), row["hash"] or "")


def regions(c):
    r = json_setting(c, "PRICES", None)
    return r if isinstance(r, list) and r else DEFAULT_REGIONS


def region_of(c, ctry):
    rs, cc = regions(c), country(ctry)
    return (next((x for x in rs if x.get("id") != "OTHER" and cc in (x.get("countries") or [])), None)
            or next((x for x in rs if x.get("id") == "OTHER"), None) or rs[0])


def exam_cfg(c):
    cfg = json_setting(c, "EXAM_CFG", {}) or {}
    return {"lock": cfg.get("lock") is not False, "price": cfg.get("price") or "£3", "how": cfg.get("how") or "",
            "refReward": REF_REWARD, "refDays": REF_DAYS,
            "regions": [{"id": r.get("id"), "name": r.get("name"), "countries": r.get("countries") or [], "cur": r.get("cur"),
                         "amount": r.get("amount"), "label": r.get("label"), "how": r.get("how") or ""} for r in regions(c)]}


def domains(c):
    return json_setting(c, "UNI_DOMAINS", None) or DEFAULT_DOMAINS


_packs = {"v": None, "at": 0}


def packs(c=None):
    """Private university packs: from PACKS_FILE / PACKS_JSON if set, otherwise from the database (copied over by migrate.py)."""
    import time as _t
    if _packs["v"] is not None and (settings.PACKS_FILE or settings.PACKS_JSON or _t.time() - _packs["at"] < 600):
        return _packs["v"]
    v = {}
    try:
        if settings.PACKS_JSON:
            v = json.loads(settings.PACKS_JSON)
        elif settings.PACKS_FILE:
            with open(settings.PACKS_FILE, encoding="utf-8") as f:
                v = json.load(f)
        else:
            from .db import engine
            if c is None:
                with engine.connect() as c2:
                    v = json_setting(c2, "PACKS", {}) or {}
            else:
                v = json_setting(c, "PACKS", {}) or {}
    except (OSError, ValueError):
        v = {}
    _packs.update(v=v, at=_t.time() if v else _t.time() - 570)   # retry soon if nothing was found yet
    return v


def unis(c):
    d, by, pk = domains(c), {}, packs(c)
    for dom, name in d.items():
        by.setdefault(name, []).append(dom)
    return [{"name": n, "domains": ds, "pack": n in pk} for n, ds in by.items()]


def audience(c, a):
    a = js_str(a).strip()
    if not a:
        return ""
    return a if any(u["name"] == a for u in unis(c)) else None


# ---------- users & sessions ----------
def profile(r):
    return {"id": r["id"], "pid": pid(r["id"]), "name": r["name"], "mobile": r["mobile"], "email": r["email"],
            "university": r["uni"] or "", "course": r["course"] or "", "lb": bool(r["lb"]), "created": r["created"],
            "uniEmail": r["uni_email"] or "", "verifiedUni": r["vuni"] or "", "examAccess": bool(r["exam"]),
            "examUsed": bool(r["exam_used"]), "refCode": ref_code(r["id"]), "site": site(r["site"]), "country": r["country"] or ""}


def progress(r):
    try:
        return json.loads(r["prog"]) if r["prog"] else None
    except ValueError:
        return None


def new_session(c, uid):
    token = secrets.token_hex(32)
    h = sha(token)
    c.execute(insert(sessions).values(token_hash=h, uid=uid, created=iso(), expires=now_ms() + SESSION_DAYS * DAY))
    cache.put("tok:" + h, uid, 300)
    return token


def auth(c, token):
    token = js_str(token)
    if not re.fullmatch(r"[a-f0-9]{64}", token):
        return None
    h = sha(token)
    hit = cache.get("tok:" + h)
    if hit:
        return hit
    r = c.execute(select(sessions.c.uid, sessions.c.expires).where(sessions.c.token_hash == h)).first()
    if r and r.expires > now_ms():
        cache.put("tok:" + h, r.uid, 300)
        return r.uid
    return None


def kill_sessions(c, uid):
    for r in c.execute(select(sessions.c.token_hash).where(sessions.c.uid == uid)):
        cache.remove("tok:" + r.token_hash)
    c.execute(delete(sessions).where(sessions.c.uid == uid))


def user_by(c, col, val, lock=False):
    q = select(users).where(col == val)
    if lock:
        q = q.with_for_update()
    return c.execute(q).mappings().first()


def me(ctx, b, lock=False):
    uid = auth(ctx.c, b.get("token"))
    return user_by(ctx.c, users.c.id, uid, lock) if uid else None


def set_user(c, uid, **vals):
    c.execute(update(users).where(users.c.id == uid).values(**vals))


def six_digit_code():
    return str(100000 + secrets.randbelow(900000))


def sale(c, uid, name, amount, how, cur="GBP"):
    c.execute(insert(events).values(ts=now_ms(), ins=now_ms(), uid=uid, name=text(name, 40), ev="unlock", s=amount, cur=cur or "GBP", k=text(how, 60)))


def money_event(c, uid, name, ev, amount, cur, detail):
    c.execute(insert(events).values(ts=now_ms(), ins=now_ms(), uid=uid, name=text(name, 40), ev=ev, s=amount, cur=cur or None, k=text(detail, 60)))


def admin_ok(ctx, b):
    key = settings.ADMIN_KEY or get_setting(ctx.c, "ADMIN_KEY")
    k = "adminfail:" + (ctx.ip or "?")
    if too_many(ctx.c, k, 20):
        return False
    if key and hmac.compare_digest(js_str(b.get("key")), key):
        return True
    bump(ctx.c, k, 900)
    return False


def admin(fn):
    def wrapped(ctx, b):
        if not admin_ok(ctx, b):
            return fail("bad_key")
        return fn(ctx, b)
    wrapped.__name__ = fn.__name__
    return action(wrapped)


# ======================================================================
# Accounts
# ======================================================================
@action
def ping(ctx, b):
    return {"ok": True, "app": APP}


@action
def signup(ctx, b):
    c = ctx.c
    name, mob, mail = text(b.get("name"), 40), mobile(b.get("mobile")), email(b.get("email"))
    uni, course, p = text(b.get("university"), 80), text(b.get("course"), 80), pw(b.get("pw"))
    if len(name) < 2: return fail("bad_name")
    if not mob: return fail("bad_mobile")
    if not mail: return fail("bad_email")
    if len(uni) < 2: return fail("bad_university")
    if len(course) < 2: return fail("bad_course")
    if not p: return fail("bad_password")
    if b.get("consent") is not True: return fail("no_consent")
    ipk = "signup:" + (ctx.ip or "?")
    if too_many(c, ipk, 300):         # generous: a whole class can sign up together from one campus network
        return fail("too_many_attempts")
    if user_by(c, users.c.mobile, mob): return fail("mobile_taken")
    if user_by(c, users.c.email, mail): return fail("email_taken")
    uid, salt = uid_new(), uid_new()
    ref = re.sub(r"[^0-9A-F]", "", js_str(b.get("ref")).upper())
    ref_by = ""
    if len(ref) == 8:
        r = c.execute(select(users.c.id).where(users.c.ref_code == ref)).first()
        ref_by = r.id if r else ""
    row = dict(id=uid, created=iso(), name=name, mobile=mob, email=mail, uni=uni, course=course, hash=hash_pw(c, p, salt), salt=salt,
               lb=b.get("lb") is not False, prog="", prog_at="", last=iso(), consent=iso(), ref_by=ref_by, ref_code=ref_code(uid),
               src=src(b.get("src")) or ("referral" if ref_by else "direct"), dev=dev(b.get("dev")), site=site(b.get("site")),
               country=country(b.get("country")), cday=now_ms() // DAY, uni_email="", vuni="", vcode="", vexp="", exam="", exam_used="", rcode="", rexp="")
    try:
        with c.begin_nested():
            c.execute(insert(users).values(**row))
    except IntegrityError:
        return fail("mobile_taken") if user_by(c, users.c.mobile, mob) else fail("email_taken")
    bump(c, ipk, 3600)
    cache.remove("leaderboard:")
    return {"ok": True, "token": new_session(c, uid), "profile": profile(row), "progress": None}


@action
def login(ctx, b):
    c = ctx.c
    mob, p = mobile(b.get("mobile")), pw(b.get("pw"))
    if not mob or not p: return fail("wrong_login")
    key, ipk = "fail:" + mob, "ipfail:" + (ctx.ip or "?")
    if too_many(c, key, 5) or too_many(c, ipk, 100): return fail("too_many_attempts")
    r = user_by(c, users.c.mobile, mob)
    if not r or not pw_matches(c, p, r):
        bump(c, key, 900); bump(c, ipk, 900)
        return fail("wrong_login")
    forget(c, key)
    set_user(c, r["id"], last=iso())
    return {"ok": True, "token": new_session(c, r["id"]), "profile": profile(r), "progress": progress(r)}


@action
def me_(ctx, b):
    m = me(ctx, b)
    if not m: return fail("auth")
    return {"ok": True, "profile": profile(m), "progress": progress(m)}


@action
def logout(ctx, b):
    t = js_str(b.get("token"))
    if re.fullmatch(r"[a-f0-9]{64}", t):
        h = sha(t)
        cache.remove("tok:" + h)
        ctx.c.execute(delete(sessions).where(sessions.c.token_hash == h))
    return {"ok": True}


@action
def saveProgress(ctx, b):
    s = json.dumps(b.get("progress") or {}, ensure_ascii=False, separators=(",", ":"))
    if len(s) > MAX_PROGRESS_CHARS: return fail("progress_too_big")
    uid = auth(ctx.c, b.get("token"))
    if not uid: return fail("auth")
    if not ctx.c.execute(update(users).where(users.c.id == uid).values(prog=s, prog_at=iso())).rowcount:
        return fail("auth")
    return {"ok": True}


def _event_ts(v):
    n = js_num(v) if v not in (None, "", 0) else None
    now = now_ms()
    return int(n) if n and 1_500_000_000_000 < n < now + DAY else now


@action
def events_(ctx, b):
    m = me(ctx, b)
    if not m: return fail("auth")
    lst = [e for e in (b.get("events") or [])[:300] if isinstance(e, dict) and e.get("t") in ACTIVITY] if isinstance(b.get("events"), list) else []
    if not lst: return {"ok": True, "saved": 0}
    ins = now_ms()
    rows = [dict(ts=_event_ts(e.get("ts")), ins=ins, uid=m["id"], name=m["name"], ev=e["t"], topic=text(e.get("topic"), 20) or None,
                 qid=num_or_none(e.get("id")), ok=1 if e.get("ok") is True else 0 if e.get("ok") is False else None,
                 s=num_or_none(e.get("s")), n=num_or_none(e.get("n")), k=text(e.get("k"), 30) or None) for e in lst]
    ctx.c.execute(insert(events), rows)
    return {"ok": True, "saved": len(rows)}


@action
def updateProfile(ctx, b):
    m = me(ctx, b, lock=True)
    if not m: return fail("auth")
    name, uni, course = text(b.get("name"), 40), text(b.get("university"), 80), text(b.get("course"), 80)
    if len(name) < 2: return fail("bad_name")
    if len(uni) < 2: return fail("bad_university")
    if len(course) < 2: return fail("bad_course")
    vals = dict(name=name, uni=uni, course=course, lb=b.get("lb") is not False)
    if country(b.get("country")): vals["country"] = country(b.get("country"))
    set_user(ctx.c, m["id"], **vals)
    cache.remove("leaderboard:")
    return {"ok": True, "profile": profile({**m, **vals})}


@action
def changePassword(ctx, b):
    m = me(ctx, b, lock=True)
    if not m: return fail("auth")
    old, new = pw(b.get("oldPw")), pw(b.get("newPw"))
    if not pw_matches(ctx.c, old, m): return fail("wrong_password")
    if not new: return fail("bad_password")
    salt = uid_new()
    set_user(ctx.c, m["id"], salt=salt, hash=hash_pw(ctx.c, new, salt))
    kill_sessions(ctx.c, m["id"])
    return {"ok": True, "token": new_session(ctx.c, m["id"])}


@action
def forgot(ctx, b):
    c = ctx.c
    mob = mobile(b.get("mobile"))
    if not mob: return fail("bad_mobile")
    key = "forgot:" + mob
    if too_many(c, key, 3): return fail("too_many_attempts")
    bump(c, key, 3600)
    r = user_by(c, users.c.mobile, mob, lock=True)
    if r:
        code = six_digit_code()
        set_user(c, r["id"], rcode=sha(code + "|" + r["salt"]), rexp=iso(now_ms() + 15 * 60000))
        ctx.mail(r["email"], brand(r["site"]) + ": your password reset code",
                 "Hi " + r["name"] + ",\n\nYour " + brand(r["site"]) + " password reset code is: " + code +
                 "\n\nIt expires in 15 minutes. If you did not ask for this, ignore this email; your password has not changed.")
    return {"ok": True}     # same answer whether or not the number exists


@action
def reset(ctx, b):
    c = ctx.c
    mob, code, p = mobile(b.get("mobile")), re.sub(r"\D", "", js_str(b.get("code"))), pw(b.get("pw"))
    if not mob or len(code) != 6: return fail("bad_code")
    if not p: return fail("bad_password")
    key = "resetfail:" + mob
    if too_many(c, key, 5): return fail("too_many_attempts")
    r = user_by(c, users.c.mobile, mob, lock=True)
    if (not r or not r["rcode"] or (parse_ms(r["rexp"]) or 0) < now_ms()
            or not hmac.compare_digest(sha(code + "|" + r["salt"]), r["rcode"])):
        bump(c, key, 900)
        return fail("bad_code")
    salt = uid_new()
    set_user(c, r["id"], salt=salt, hash=hash_pw(c, p, salt), rcode="", rexp="")
    forget(c, key); forget(c, "fail:" + mob)
    kill_sessions(c, r["id"])
    return {"ok": True, "token": new_session(c, r["id"]), "profile": profile(r), "progress": progress(r)}


@action
def deleteAccount(ctx, b):
    c = ctx.c
    m = me(ctx, b, lock=True)
    if not m: return fail("auth")
    if not pw_matches(c, pw(b.get("pw")), m): return fail("wrong_password")
    uid = m["id"]
    kill_sessions(c, uid)
    for r in c.execute(select(proctor.c.file).where(proctor.c.uid == uid)).all():
        store().delete(r.file)
    c.execute(delete(proctor).where(proctor.c.uid == uid))
    c.execute(delete(events).where(events.c.uid == uid))
    c.execute(delete(user_stats).where(user_stats.c.uid == uid))
    c.execute(delete(day_activity).where(day_activity.c.uid == uid))
    c.execute(delete(users).where(users.c.id == uid))
    cache.remove("leaderboard:")
    return {"ok": True}


# ======================================================================
# Public config, announcements and admin-written questions
# ======================================================================
def q_out(r):
    try:
        o, a = json.loads(r["o"]), json.loads(r["a"])
    except (TypeError, ValueError):
        return None
    q = {"id": r["id"], "t": r["t"], "type": r["type"], "p": r["p"], "e": r["e"], "custom": True, "audience": r["audience"] or ""}
    if q["type"] == "match":
        q["l"], q["r"] = o.get("l"), o.get("r")
    else:
        q["o"], q["a"] = o, a
    return q


def news_out(r, with_aud=False):
    o = {"id": r["id"], "created": r["created"], "title": r["title"], "message": r["message"], "level": r["level"] or "info"}
    if with_aud:
        o["audience"] = r["audience"] or ""
    return o


def live_news(c):
    now = now_ms()
    return [r for r in c.execute(select(news).order_by(news.c.seq.desc())).mappings() if not r["expires"] or (parse_ms(r["expires"]) or 0) > now]


@action
def config(ctx, b):
    hit = cache.get("config")
    if hit:
        return hit
    c = ctx.c
    nw = [news_out(r) for r in live_news(c) if not r["audience"]]
    qs = [q for q in (q_out(r) for r in c.execute(select(questions).where(or_(questions.c.audience == "", questions.c.audience.is_(None)))
                                                  .order_by(questions.c.seq)).mappings()) if q]
    out = {"ok": True, "news": nw, "questions": qs, "hidden": json_setting(c, "HIDDEN_Q", []), "unis": unis(c), "exam": exam_cfg(c), "at": now_ms()}
    cache.put("config", out, 60)
    return out


@admin
def adminAnnounce(ctx, b):
    c = ctx.c
    title, message = text(b.get("title"), 80), text(b.get("message"), 600)
    level = "important" if b.get("level") == "important" else "info"
    aud = audience(c, b.get("audience"))
    if aud is None: return fail("bad_audience")
    days = max(1, min(60, js_num(b.get("days")) or 7))
    if len(title) < 2: return fail("bad_title")
    nid = uid_new()[:8]
    c.execute(insert(news).values(id=nid, created=iso(), title=title, message=message, level=level, expires=iso(now_ms() + int(days * DAY)), audience=aud))
    cache.remove("config")
    return {"ok": True, "id": nid}


@admin
def adminDeleteAnnouncement(ctx, b):
    ctx.c.execute(delete(news).where(news.c.id == js_str(b.get("id"))))
    cache.remove("config")
    return {"ok": True}


def q_in(q):
    q = q if isinstance(q, dict) else {}
    t = q.get("t") if q.get("t") in TOPIC_KEYS else None
    typ = q.get("type") if q.get("type") in ("single", "multi", "match") else None
    p, e = text(q.get("p"), 600), text(q.get("e"), 600)
    if not t: return {"error": "bad_topic"}
    if not typ: return {"error": "bad_type"}
    if len(p) < 5: return {"error": "bad_prompt"}
    if typ == "match":
        l = [x for x in (plain(x, 160) for x in (q.get("l") or [])) if x]
        r = [x for x in (plain(x, 160) for x in (q.get("r") or [])) if x]
        if len(l) < 2 or len(l) != len(r) or len(l) > 8: return {"error": "bad_pairs"}
        return {"t": t, "type": typ, "p": p, "o": {"l": l, "r": r}, "a": None, "e": e}
    o = [x for x in (plain(x, 200) for x in (q.get("o") or [])) if x]
    if len(o) < 2 or len(o) > 8: return {"error": "bad_options"}
    if typ == "single":
        a = js_num(q.get("a"))
        if a is None or not (0 <= a < len(o)) or not float(a).is_integer(): return {"error": "bad_answer"}
        return {"t": t, "type": typ, "p": p, "o": o, "a": int(a), "e": e}
    a = []
    for x in (q.get("a") or []):
        n = js_num(x)
        if n is not None and float(n).is_integer() and 0 <= n < len(o) and int(n) not in a:
            a.append(int(n))
    if not a: return {"error": "bad_answer"}
    return {"t": t, "type": typ, "p": p, "o": o, "a": a, "e": e}


@admin
def adminSaveQuestion(ctx, b):
    c = ctx.c
    q = q_in(b.get("question"))
    if q.get("error"): return fail(q["error"])
    aud = audience(c, (b.get("question") or {}).get("audience"))
    if aud is None: return fail("bad_audience")
    qid = int(js_num((b.get("question") or {}).get("id")) or 0)
    vals = dict(updated=iso(), t=q["t"], type=q["type"], p=q["p"], o=json.dumps(q["o"], ensure_ascii=False), a=json.dumps(q["a"]), e=q["e"], audience=aud)
    if qid and c.execute(update(questions).where(questions.c.id == qid).values(**vals)).rowcount:
        pass
    else:
        mx = c.execute(select(func.max(questions.c.id))).scalar() or 0
        qid = max(999, mx) + 1
        c.execute(insert(questions).values(id=qid, **vals))
    cache.remove("config")
    return {"ok": True, "id": qid}


@admin
def adminDeleteQuestion(ctx, b):
    n = js_num(b.get("id"))
    if n is not None:
        ctx.c.execute(delete(questions).where(questions.c.id == n))
    cache.remove("config")
    return {"ok": True}


@admin
def adminHideBuiltin(ctx, b):
    c = ctx.c
    hidden = json_setting(c, "HIDDEN_Q", [])
    n = js_num(b.get("id"))
    if n is None or not (0 <= n < 1000): return fail("bad_question")
    n = int(n) if float(n).is_integer() else n
    hidden = [x for x in hidden if x != n]
    if b.get("hide"): hidden.append(n)
    set_setting(c, "HIDDEN_Q", json.dumps(hidden))
    cache.remove("config")
    return {"ok": True, "hidden": hidden}


# ======================================================================
# University verification & private packs
# ======================================================================
@action
def verifyStart(ctx, b):
    c = ctx.c
    mail = email(b.get("email"))
    if not mail: return fail("bad_email")
    uni = domains(c).get(mail.split("@")[1])
    if not uni: return fail("unknown_domain")
    m = me(ctx, b, lock=True)
    if not m: return fail("auth")
    key = "verify:" + m["id"]
    if too_many(c, key, 4): return fail("too_many_attempts")
    other = user_by(c, users.c.uni_email, mail)
    if other and other["id"] != m["id"] and other["vuni"]: return fail("email_used")
    bump(c, key, 3600)
    code = six_digit_code()
    set_user(c, m["id"], uni_email=mail, vcode=sha(code + "|" + m["salt"]), vexp=iso(now_ms() + 15 * 60000))
    ctx.mail(mail, APP + ": verify your university email",
             "Hi " + m["name"] + ",\n\nYour code to verify your " + uni + " email is: " + code +
             "\n\nIt expires in 15 minutes. If you did not request this, you can ignore this email.")
    return {"ok": True, "university": uni}


@action
def verifyConfirm(ctx, b):
    c = ctx.c
    m = me(ctx, b, lock=True)
    if not m: return fail("auth")
    key = "vfail:" + m["id"]
    if too_many(c, key, 5): return fail("too_many_attempts")
    code = re.sub(r"\D", "", js_str(b.get("code")))
    if (len(code) != 6 or not m["vcode"] or (parse_ms(m["vexp"]) or 0) < now_ms()
            or not hmac.compare_digest(sha(code + "|" + m["salt"]), m["vcode"])):
        bump(c, key, 900)
        return fail("bad_code")
    uni = domains(c).get((m["uni_email"] or "").split("@")[-1])
    if not uni: return fail("unknown_domain")
    set_user(c, m["id"], vuni=uni, vcode="", vexp="")
    cache.remove("leaderboard:")
    return {"ok": True, "profile": profile({**m, "vuni": uni})}


@action
def pack(ctx, b):
    c = ctx.c
    m = me(ctx, b)
    if not m: return fail("auth")
    uni = m["vuni"] or ""
    if not uni: return fail("not_verified")
    pk = packs(c).get(uni) or {}
    extra = [q for q in (q_out(r) for r in c.execute(select(questions).where(questions.c.audience == uni).order_by(questions.c.seq)).mappings()) if q]
    nw = [news_out(r) for r in live_news(c) if r["audience"] == uni]
    return {"ok": True, "uni": uni, "title": pk.get("title", ""), "exam": pk.get("exam"), "notes": pk.get("notes", ""),
            "questions": pk.get("questions", []), "extra": extra, "news": nw}


@admin
def adminContent(ctx, b):
    c = ctx.c
    return {"ok": True,
            "questions": [q for q in (q_out(r) for r in c.execute(select(questions).order_by(questions.c.seq)).mappings()) if q],
            "news": [news_out(r, True) for r in live_news(c)]}


@admin
def adminSetAccess(ctx, b):
    c = ctx.c
    r = user_by(c, users.c.id, js_str(b.get("id")))
    if not r: return fail("no_user")
    uni = js_str(b.get("uni"))
    if uni and not any(u["name"] == uni for u in unis(c)): return fail("bad_university")
    vals = {"vuni": uni}
    if not uni: vals.update(vcode="", vexp="")
    set_user(c, r["id"], **vals)
    cache.remove("leaderboard:")
    return {"ok": True, "id": r["id"], "verifiedUni": uni}


@admin
def adminSetDomains(ctx, b):
    c = ctx.c
    d = b.get("domains")
    if isinstance(d, dict):
        clean = {}
        for k in list(d.keys())[:50]:
            dom, name = js_str(k).lower().lstrip("@").strip(), plain(d[k], 80)
            if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", dom) and len(name) >= 3:
                clean[dom] = name
        set_setting(c, "UNI_DOMAINS", json.dumps(clean))
        cache.remove("config")
    return {"ok": True, "domains": domains(c), "packs": list(packs(c).keys())}


# ======================================================================
# Exam access: coupons, admin grants, prices
# ======================================================================
def coupons_list(c):
    return [{"code": r["code"], "created": r["created"], "max": r["max"] or 1, "used": r["used"] or 0, "expires": r["expires"] or "",
             "note": r["note"] or "", "usedBy": len([x for x in (r["used_by"] or "").split(",") if x]), "value": out_num(r["value"] or 0)}
            for r in c.execute(select(coupons).order_by(coupons.c.seq.desc())).mappings()]


@action
def redeem(ctx, b):
    c = ctx.c
    m = me(ctx, b, lock=True)
    if not m: return fail("auth")
    key = "redeem:" + m["id"]
    if too_many(c, key, 8): return fail("too_many_attempts")
    if m["exam"]: return {"ok": True, "already": True, "profile": profile(m)}
    code = coupon_code(b.get("code"))
    cp = c.execute(select(coupons).where(coupons.c.code == code).with_for_update()).mappings().first() if code else None
    if not cp:
        bump(c, key, 900)
        return fail("bad_coupon")
    if cp["expires"] and (parse_ms(cp["expires"]) or 0) < now_ms(): return fail("coupon_expired")
    if (cp["used"] or 0) >= (cp["max"] or 1): return fail("coupon_used_up")
    took = c.execute(update(coupons).where(and_(coupons.c.code == code, coupons.c.used < coupons.c.max)).values(
        used=coupons.c.used + 1, used_by=case((coupons.c.used_by == "", m["id"]), else_=coupons.c.used_by + "," + m["id"]))).rowcount
    if not took: return fail("coupon_used_up")      # someone else took the last use a moment ago
    grant = "coupon " + code + " · " + iso()
    set_user(c, m["id"], exam=grant)
    sale(c, m["id"], m["name"], cp["value"] or 0, "coupon " + code)
    return {"ok": True, "profile": profile({**m, "exam": grant})}


@admin
def adminCoupons(ctx, b):
    return {"ok": True, "exam": exam_cfg(ctx.c), "coupons": coupons_list(ctx.c)}


@admin
def adminCreateCoupon(ctx, b):
    c = ctx.c
    code = coupon_code(b.get("code"))
    if b.get("code") and not code: return fail("bad_code")
    exists = lambda k: c.execute(select(coupons.c.code).where(coupons.c.code == k)).first()
    if not code:
        code = "IDTA-" + sha(uid_new())[:6].upper()
        while exists(code):
            code = "IDTA-" + sha(uid_new())[:6].upper()
    elif exists(code):
        return fail("code_exists")
    uses = max(1, min(1000, int(js_num(b.get("uses")) or 1)))
    days = max(0, min(365, int(js_num(b.get("days")) or 0)))
    c.execute(insert(coupons).values(code=code, created=iso(), max=uses, used=0, expires=iso(now_ms() + days * DAY) if days else "",
                                     note=text(b.get("note"), 80), used_by="", value=money(b.get("value"))))
    return {"ok": True, "code": code, "coupons": coupons_list(c)}


@admin
def adminDeleteCoupon(ctx, b):
    ctx.c.execute(delete(coupons).where(coupons.c.code == coupon_code(b.get("code"))))
    return {"ok": True, "coupons": coupons_list(ctx.c)}


@admin
def adminSetExam(ctx, b):
    c = ctx.c
    r = user_by(c, users.c.id, js_str(b.get("id")), lock=True)
    if not r: return fail("no_user")
    val = ("granted by admin · " + iso()) if b.get("on") else ""
    set_user(c, r["id"], exam=val)
    if b.get("on"):
        reg = region_of(c, r["country"])
        amt = (js_num(reg.get("amount")) or 0) if b.get("amount") == "region" else money(b.get("amount"))
        cur = reg.get("cur") if b.get("amount") == "region" else "GBP"
        sale(c, r["id"], r["name"], amt, "admin (paid)" if amt else "admin (free)", cur)
    elif r["exam"]:
        money_event(c, r["id"], r["name"], "revoke", 0, None, "admin")
    return {"ok": True, "id": r["id"], "examAccess": val}


def _how(v):
    return "\n".join(plain(l, 200) for l in re.split(r"\r?\n", js_str(v))).strip()[:600]


@admin
def adminSetPrices(ctx, b):
    rs = b.get("regions")
    if not isinstance(rs, list) or not rs or len(rs) > 40: return fail("bad_prices")
    out = []
    for r in rs:
        r = r if isinstance(r, dict) else {}
        amt = js_num(r.get("amount"))
        out.append({"id": re.sub(r"[^A-Z0-9_]", "", plain(r.get("id"), 12).upper()) or "R", "name": plain(r.get("name"), 40),
                    "countries": [x for x in (country(x) for x in (r.get("countries") or [])) if x][:80],
                    "cur": re.sub(r"[^A-Z]", "", plain(r.get("cur"), 3).upper()) or "USD",
                    "amount": out_num(max(0, round((amt or 0) * 100) / 100)), "label": plain(r.get("label"), 16), "how": _how(r.get("how"))})
    if not any(r["id"] == "OTHER" for r in out): return fail("bad_prices")
    set_setting(ctx.c, "PRICES", json.dumps(out, ensure_ascii=False))
    cache.remove("config")
    return {"ok": True, "exam": exam_cfg(ctx.c)}


@admin
def adminSetExamConfig(ctx, b):
    cfg = {"lock": bool(b.get("lock")), "price": plain(b.get("price"), 20) or "£3", "how": _how(b.get("how"))}
    set_setting(ctx.c, "EXAM_CFG", json.dumps(cfg, ensure_ascii=False))
    cache.remove("config")
    return {"ok": True, "exam": exam_cfg(ctx.c)}


@admin
def adminSales(ctx, b):
    c = ctx.c
    rows = c.execute(select(events.c.ts, events.c.uid, events.c.name, events.c.ev, events.c.s, events.c.cur, events.c.k)
                     .where(events.c.ev.in_(MONEY_EVENTS)).order_by(events.c.seq.desc()).limit(20000)).all()[::-1]
    accounts = c.execute(select(func.count()).select_from(users)).scalar()
    unlocked = c.execute(select(func.count()).select_from(users).where(users.c.exam != "")).scalar()
    sales = []
    for r in rows:
        cur, amt = r.cur or "GBP", r.s or 0
        sales.append({"ts": r.ts, "id": r.uid, "name": r.name, "kind": r.ev, "amount": out_num(round(amt * TO_GBP.get(cur, 0) * 100) / 100),
                      "orig": out_num(amt), "cur": cur, "how": r.k or ""})
    return {"ok": True, "exam": exam_cfg(c), "accounts": accounts, "unlocked": unlocked, "sales": sales}


# ======================================================================
# Certificates
# ======================================================================
def cert_out(r):
    return {"id": r["id"], "issued": r["issued"], "name": r["name"], "score": out_num(r["score"]), "n": out_num(r["n"]),
            "exam": r["exam"], "site": site(r["site"])}


def new_cert_id(c):
    while True:
        cid = short_id("C", 9)
        if not c.execute(select(certs.c.id).where(certs.c.id == cid)).first():
            return cid


@action
def certIssue(ctx, b):
    c = ctx.c
    m = me(ctx, b, lock=True)
    if not m: return fail("auth")
    n, score, exam = js_num(b.get("n")), js_num(b.get("score")), text(b.get("exam"), 40)
    if n is None or score is None or int(n // 1) != 30 or not (21 <= score // 1 <= 30) or len(exam) < 3: return fail("not_eligible")
    n, score = 30, int(score // 1)
    key = "cert:" + m["id"]
    if too_many(c, key, 10): return fail("too_many_attempts")
    have = c.execute(select(certs).where(and_(certs.c.uid == m["id"], certs.c.exam == exam, certs.c.score >= score))
                     .order_by(certs.c.seq)).mappings().first()
    if have: return {"ok": True, "cert": cert_out(have)}
    bump(c, key, 86400)
    row = dict(id=new_cert_id(c), issued=iso(), uid=m["id"], name=text(m["name"], 40), score=score, n=n, exam=exam, site=site(b.get("site") or m["site"]))
    c.execute(insert(certs).values(**row))
    return {"ok": True, "cert": cert_out(row)}


@action
def cert(ctx, b):
    cid = re.sub(r"[^A-Z0-9]", "", js_str(b.get("id")).upper())
    if len(cid) != 10: return fail("no_cert")
    r = ctx.c.execute(select(certs).where(certs.c.id == cid)).mappings().first()
    return {"ok": True, "cert": cert_out(r)} if r else fail("no_cert")


@action
def myCerts(ctx, b):
    m = me(ctx, b)
    if not m: return fail("auth")
    return {"ok": True, "certs": [cert_out(r) for r in ctx.c.execute(select(certs).where(certs.c.uid == m["id"]).order_by(certs.c.seq.desc())).mappings()]}


# ======================================================================
# Payments: the student uploads a screenshot + transaction reference, the organiser approves
# ======================================================================
def _check_image(data, mime):
    """Make sure the upload really is a picture (not a renamed file), return (ok, detected mime)."""
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = 40_000_000
        im = Image.open(io.BytesIO(data))
        fmt = (im.format or "").upper()
        w, h = im.size
        im.verify()
    except Exception:
        return False, None
    det = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(fmt)
    if not det or w < 100 or h < 100:
        return False, None
    return True, det


def pay_out(r):
    return {"id": r["id"], "submitted": r["submitted"], "uid": r["uid"], "name": r["name"], "mobile": r["mobile"],
            "amount": out_num(r["amount"] or 0), "ref": r["ref"] or "", "status": r["status"], "decided": r["decided"] or "",
            "note": r["note"] or "", "cur": r["cur"] or "GBP", "flags": [f for f in (r["flags"] or "").split("\n") if f]}


@action
def paySubmit(ctx, b):
    c = ctx.c
    m = me(ctx, b, lock=True)
    if not m: return fail("auth")
    if m["exam"]: return fail("already_unlocked")
    mt, data = js_str(b.get("mime")), js_str(b.get("image"))
    if not re.fullmatch(r"image/(jpeg|png|webp)", mt) or len(data) < 200 or len(data) > 2_800_000 or not b64ok(data):
        return fail("bad_image")
    key = "pay:" + m["id"]
    if too_many(c, key, 6): return fail("too_many_attempts")
    if c.execute(select(payments.c.id).where(and_(payments.c.uid == m["id"], payments.c.status == "pending"))).first():
        return fail("pay_pending")
    ref, rn = text(b.get("ref"), 60), ref_norm(b.get("ref"))
    if len(rn) < 6: return fail("bad_ref")
    try:
        raw = base64.b64decode(data, validate=True)
    except Exception:
        return fail("bad_image")
    good, det = _check_image(raw, mt)
    if not good: return fail("bad_image")
    bump(c, key, 86400)
    img_hash = hashlib.sha256(raw).hexdigest()
    flags = []
    # A screenshot or reference that already unlocked someone else's account is refused outright.
    for r in c.execute(select(payments).where(or_(payments.c.img_hash == img_hash, payments.c.ref_norm == rn)).order_by(payments.c.seq)).mappings():
        same_img = r["img_hash"] == img_hash
        what = "screenshot" if same_img else "transaction reference"
        if r["uid"] != m["id"] and r["status"] == "approved":
            ctx.notify("blocked duplicate payment from " + m["name"],
                       m["name"] + " (+" + m["mobile"] + ") tried to use a " + what + " that already unlocked " + r["name"] + "'s account (payment " + r["id"] + "). It was refused automatically.")
            return fail("duplicate_payment")
        who = "this student" if r["uid"] == m["id"] else r["name"]
        flags.append("Same " + what + " as payment " + r["id"] + " (" + who + ", " + r["status"] + ")")
    reg = region_of(c, m["country"])
    pid_ = short_id("P", 8)
    ext = {"image/png": ".png", "image/webp": ".webp"}.get(det, ".jpg")
    fkey = "payments/" + pid_ + "-" + re.sub(r"[^A-Za-z0-9]+", "_", m["name"])[:30] + ext
    store().put(fkey, raw, det)
    now = iso()
    c.execute(insert(payments).values(id=pid_, submitted=now, uid=m["id"], name=text(m["name"], 40), mobile="+" + m["mobile"],
                                      amount=js_num(reg.get("amount")) or 0, ref=ref, ref_norm=rn, file=fkey, img_hash=img_hash,
                                      status="pending", decided="", note="", cur=reg.get("cur"), flags="\n".join(flags), ip=ctx.ip[:64]))
    to = settings.NOTIFY_EMAIL or get_setting(c, "NOTIFY_EMAIL")
    if to:
        ctx.mail(to, brand(m["site"]) + ": payment to check from " + m["name"],
                 m["name"] + " (+" + m["mobile"] + ", " + (m["country"] or "country not set") + ") uploaded a payment screenshot for " +
                 js_str(reg.get("label")) + " (reference: " + ref + ")." + ("\n\nWarning: " + "; ".join(flags) if flags else "") +
                 "\n\nOpen your app, go to Admin -> Exam & coupons, and approve or reject it.")
    return {"ok": True, "payment": {"id": pid_, "status": "pending", "submitted": now}}


@action
def payStatus(ctx, b):
    m = me(ctx, b)
    if not m: return fail("auth")
    last = ctx.c.execute(select(payments).where(payments.c.uid == m["id"]).order_by(payments.c.seq.desc()).limit(1)).mappings().first()
    return {"ok": True, "profile": profile(m),
            "payment": {"id": last["id"], "submitted": last["submitted"], "status": last["status"], "note": last["note"] or ""} if last else None}


@admin
def adminPayments(ctx, b):
    lst = [pay_out(r) for r in ctx.c.execute(select(payments).order_by(payments.c.seq.desc())).mappings()]
    return {"ok": True, "pending": [p for p in lst if p["status"] == "pending"], "recent": [p for p in lst if p["status"] != "pending"][:30]}


@admin
def adminPaymentImage(ctx, b):
    r = ctx.c.execute(select(payments.c.file).where(payments.c.id == js_str(b.get("id")))).first()
    if not r: return fail("no_payment")
    data, mime = store().get(r.file) if r.file else (None, None)
    if data is None: return fail("no_image")
    return {"ok": True, "mime": mime, "image": base64.b64encode(data).decode()}


@admin
def adminDecidePayment(ctx, b):
    c = ctx.c
    p = c.execute(select(payments).where(payments.c.id == js_str(b.get("id"))).with_for_update()).mappings().first()
    if not p: return fail("no_payment")
    if p["status"] != "pending": return fail("already_decided")
    ok, note = bool(b.get("approve")), text(b.get("note"), 120)
    amount = money(b.get("amount")) if b.get("amount") is not None else (p["amount"] or 0)
    cur = p["cur"] or "GBP"
    if not c.execute(update(payments).where(and_(payments.c.id == p["id"], payments.c.status == "pending"))
                     .values(status="approved" if ok else "rejected", decided=iso(), note=note)).rowcount:
        return fail("already_decided")      # a second click or a second admin got there first
    u = user_by(c, users.c.id, p["uid"], lock=True)
    if u:
        if ok and not u["exam"]:
            set_user(c, u["id"], exam="payment " + p["id"] + " · " + iso())
            sale(c, u["id"], u["name"], amount, "payment " + p["id"] + (" · " + p["ref"] if p["ref"] else ""), cur)
        ctx.mail(u["email"], brand(u["site"]) + (": your course exams are unlocked" if ok else ": we could not confirm your payment"),
                 "Hi " + u["name"] + ",\n\n" + ("Your payment is confirmed and the course exams are unlocked. Open the app and go to the Mock exam tab. Good luck!" if ok else
                 "We could not confirm your payment" + (": " + note if note else "") + ".\n\nOpen the Mock exam tab to upload a clearer screenshot, or contact the organiser."))
    return {"ok": True}


# ======================================================================
# Refunds: allowed only before the first course exam is started
# ======================================================================
def money_for(c, uid):
    """Amount paid for the latest unlock, whether it was refunded, and how many different days they studied."""
    o = {"paid": 0, "refunded": False, "cur": "GBP"}
    for r in c.execute(select(events.c.ev, events.c.s, events.c.cur).where(and_(events.c.uid == uid, events.c.ev.in_(("unlock", "refund"))))
                       .order_by(events.c.seq)):
        if r.ev == "unlock":
            o.update(paid=r.s or 0, cur=r.cur or "GBP", refunded=False)
        else:
            o["refunded"] = True
    o["days"] = c.execute(select(func.count(distinct(events.c.ts // DAY))).where(and_(events.c.uid == uid, events.c.ev.in_(ACTIVITY)))).scalar() or 0
    return o


@action
def examStarted(ctx, b):
    m = me(ctx, b, lock=True)
    if not m: return fail("auth")
    if m["exam"] and not m["exam_used"]:
        set_user(ctx.c, m["id"], exam_used=iso())
    return {"ok": True}


def refund_out(r, full=False):
    o = {"id": r["id"], "requested": r["requested"], "amount": out_num(r["amount"] or 0), "cur": r["cur"] or "GBP", "status": r["status"], "note": r["note"] or ""}
    if full:
        o.update(name=r["name"], mobile=r["mobile"], to=r["to"], reason=r["reason"] or "", decided=r["decided"] or "")
    return o


@action
def myPurchase(ctx, b):
    c = ctx.c
    m = me(ctx, b)
    if not m: return fail("auth")
    mon = money_for(c, m["id"])
    rf = c.execute(select(refunds).where(refunds.c.uid == m["id"]).order_by(refunds.c.seq.desc()).limit(1)).mappings().first()
    return {"ok": True, "access": bool(m["exam"]), "how": m["exam"] or "", "paid": out_num(mon["paid"]), "cur": mon["cur"], "examUsed": bool(m["exam_used"]),
            "refundable": bool(m["exam"]) and not m["exam_used"] and mon["paid"] > 0 and not mon["refunded"],
            "refund": refund_out(rf) if rf else None}


@action
def refundRequest(ctx, b):
    c = ctx.c
    m = me(ctx, b, lock=True)
    if not m: return fail("auth")
    mon = money_for(c, m["id"])
    if not m["exam"] or not mon["paid"] > 0 or mon["refunded"]: return fail("not_refundable")
    if m["exam_used"]: return fail("exam_used")
    to = plain(b.get("to"), 100)
    if len(to) < 5: return fail("bad_refund_to")
    if c.execute(select(refunds.c.id).where(and_(refunds.c.uid == m["id"], refunds.c.status == "pending"))).first(): return fail("refund_pending")
    rid = short_id("R", 8)
    c.execute(insert(refunds).values(id=rid, requested=iso(), uid=m["id"], name=text(m["name"], 40), mobile="+" + m["mobile"], amount=mon["paid"],
                                     to=to, reason=text(b.get("reason"), 200), status="pending", decided="", note="", cur=mon["cur"]))
    set_user(c, m["id"], exam="")
    money_event(c, m["id"], m["name"], "revoke", 0, None, "refund requested " + rid)
    ctx.notify("refund request from " + m["name"], m["name"] + " (+" + m["mobile"] + ") cancelled and asked for a refund of " + js_str(out_num(mon["paid"])) + " " + mon["cur"] +
               ".\nSend it to: " + to + ("\nReason: " + plain(b.get("reason"), 200) if b.get("reason") else "") +
               '\n\nTheir exam access is already removed. After you send the money, open Admin -> Exam & coupons and click "Mark refunded".')
    return {"ok": True, "profile": profile({**m, "exam": ""})}


@admin
def adminRefunds(ctx, b):
    lst = [refund_out(r, True) for r in ctx.c.execute(select(refunds).order_by(refunds.c.seq.desc())).mappings()]
    return {"ok": True, "pending": [x for x in lst if x["status"] == "pending"], "recent": [x for x in lst if x["status"] != "pending"][:30]}


@admin
def adminDecideRefund(ctx, b):
    c = ctx.c
    r = c.execute(select(refunds).where(refunds.c.id == js_str(b.get("id"))).with_for_update()).mappings().first()
    if not r: return fail("no_refund")
    if r["status"] != "pending": return fail("already_decided")
    ok, note = bool(b.get("refunded")), text(b.get("note"), 120)
    if not c.execute(update(refunds).where(and_(refunds.c.id == r["id"], refunds.c.status == "pending"))
                     .values(status="refunded" if ok else "declined", decided=iso(), note=note)).rowcount:
        return fail("already_decided")
    u = user_by(c, users.c.id, r["uid"], lock=True)
    if ok:
        money_event(c, r["uid"], r["name"], "refund", r["amount"] or 0, r["cur"] or "GBP", "refund " + r["id"])
    elif u:
        set_user(c, u["id"], exam="restored after declined refund · " + iso())
        money_event(c, r["uid"], r["name"], "restore", 0, None, "refund declined " + r["id"])
    if u:
        ctx.mail(u["email"], brand(u["site"]) + (": your refund has been sent" if ok else ": about your refund request"),
                 "Hi " + u["name"] + ",\n\n" + ("Your refund of " + js_str(out_num(r["amount"])) + " " + (r["cur"] or "GBP") + " has been sent to " + r["to"] + "." if ok else
                 "Your refund request was declined" + (": " + note if note else "") + ". Your exam access has been restored."))
    return {"ok": True}


# ======================================================================
# Referrals: £1 when a friend you invited pays and studies on 10 different days
# ======================================================================
def referrals_of(c, uid):
    friends = []
    for r in c.execute(select(users.c.id, users.c.name, users.c.created, users.c.exam).where(users.c.ref_by == uid).order_by(users.c.seq)):
        o = money_for(c, r.id)
        paid = o["paid"] > 0 and not o["refunded"] and bool(r.exam)
        friends.append({"name": (r.name or "").split(" ")[0], "joined": r.created, "paid": paid, "days": min(o["days"], REF_DAYS),
                        "qualified": paid and o["days"] >= REF_DAYS})
    qualified = len([f for f in friends if f["qualified"]])
    earned = qualified * REF_REWARD
    paid_out = pending = 0
    for r in c.execute(select(payouts.c.status, payouts.c.amount).where(payouts.c.uid == uid)):
        if r.status == "paid": paid_out += r.amount or 0
        elif r.status == "pending": pending += r.amount or 0
    return {"friends": friends, "qualified": qualified, "earned": earned, "paidOut": out_num(paid_out), "pending": out_num(pending),
            "available": out_num(max(0, earned - paid_out - pending))}


@action
def myReferrals(ctx, b):
    m = me(ctx, b)
    if not m: return fail("auth")
    return {"ok": True, "code": ref_code(m["id"]), "reward": REF_REWARD, "days": REF_DAYS, **referrals_of(ctx.c, m["id"])}


@action
def referralPayout(ctx, b):
    c = ctx.c
    m = me(ctx, b, lock=True)
    if not m: return fail("auth")
    r, to = referrals_of(c, m["id"]), plain(b.get("to"), 100)
    if r["available"] < REF_REWARD: return fail("nothing_to_pay")
    if len(to) < 5: return fail("bad_refund_to")
    xid = short_id("X", 8)
    c.execute(insert(payouts).values(id=xid, requested=iso(), uid=m["id"], name=text(m["name"], 40), mobile="+" + m["mobile"], amount=r["available"],
                                     to=to, status="pending", decided="", note=""))
    ctx.notify("referral payout request from " + m["name"], m["name"] + " (+" + m["mobile"] + ") asked for their referral reward of £" + js_str(r["available"]) +
               ".\nSend it to: " + to + '\n\nAfter you pay, open Admin -> Exam & coupons and click "Mark paid".')
    return {"ok": True, **referrals_of(c, m["id"])}


@admin
def adminReferrals(ctx, b):
    c = ctx.c
    names = {r.id: r.name for r in c.execute(select(users.c.id, users.c.name))}
    refs = [r[0] for r in c.execute(select(users.c.ref_by, func.count().label("n")).where(users.c.ref_by != "").group_by(users.c.ref_by)
                                     .order_by(func.count().desc()).limit(200))]
    top = []
    for uid in refs:
        r = referrals_of(c, uid)
        top.append({"name": names.get(uid, "(deleted)"), "invited": len(r["friends"]), "qualified": r["qualified"], "earned": r["earned"],
                    "paid": r["paidOut"], "pending": r["pending"]})
    top.sort(key=lambda x: (-x["qualified"], -x["invited"]))
    lst = [{"id": r["id"], "requested": r["requested"], "name": r["name"], "mobile": r["mobile"], "amount": out_num(r["amount"] or 0), "to": r["to"],
            "status": r["status"], "decided": r["decided"] or "", "note": r["note"] or ""}
           for r in c.execute(select(payouts).order_by(payouts.c.seq.desc())).mappings()]
    return {"ok": True, "reward": REF_REWARD, "days": REF_DAYS, "referrers": top,
            "pending": [x for x in lst if x["status"] == "pending"], "recent": [x for x in lst if x["status"] != "pending"][:30]}


@admin
def adminDecidePayout(ctx, b):
    c = ctx.c
    r = c.execute(select(payouts).where(payouts.c.id == js_str(b.get("id"))).with_for_update()).mappings().first()
    if not r: return fail("no_payout")
    if r["status"] != "pending": return fail("already_decided")
    ok = bool(b.get("paid"))
    if not c.execute(update(payouts).where(and_(payouts.c.id == r["id"], payouts.c.status == "pending"))
                     .values(status="paid" if ok else "declined", decided=iso(), note=text(b.get("note"), 120))).rowcount:
        return fail("already_decided")
    if ok:
        money_event(c, r["uid"], r["name"], "payout", r["amount"] or 0, "GBP", "referral payout " + r["id"])
    return {"ok": True}


# ======================================================================
# Analytics: first-party counts only, no cookies or third-party trackers
# ======================================================================
@action
def hit(ctx, b):
    if hits_guard.hit(20000, 86400):      # flood guard, per server instance
        return {"ok": True}
    ctx.c.execute(insert(visits).values(ts=now_ms(), src=src(b.get("src")) or "direct", dev=dev(b.get("dev")), li=bool(b.get("li")),
                                        ret=bool(b.get("ret")), site=site(b.get("site"))))
    return {"ok": True}


@admin
def adminAnalytics(ctx, b):
    """Funnel, sources, engagement and retention, computed with SQL aggregates and the daily-activity summary,
    so it stays fast with millions of answers."""
    c = ctx.c
    days = int(js_num(b.get("days")) or 30)
    days = days if days in (7, 30, 90, 365) else 30
    now = now_ms()
    frm, today = now - days * DAY, now // DAY
    frm_iso, frm_day = iso(frm), frm // DAY
    keys = [day_key(now - i * DAY) for i in range(min(days, 90) - 1, -1, -1)]
    first_key_day = (now - (len(keys) - 1) * DAY) // DAY
    want = b.get("site") if b.get("site") in ("uk", "global") else ""
    U, V, E, D, S_ = users.c, visits.c, events.c, day_activity.c, user_stats.c

    def on_site(col):
        if want == "global":
            return col == "global"
        if want == "uk":
            return or_(col != "global", col.is_(None))
        return true()

    usite, vsite = on_site(U.site), on_site(V.site)
    site_uids = select(U.id).where(usite)
    cnt = lambda q: c.execute(q).scalar() or 0
    new_u = and_(U.created >= frm_iso, usite)
    unlocked_ids = select(E.uid).where(E.ev == "unlock")
    mock_ids = select(E.uid).where(E.ev == "mock")
    funnel = {"visits": cnt(select(func.count()).where(V.ts >= frm, V.li.is_not(True), vsite)),
              "signups": cnt(select(func.count()).where(new_u)),
              "studied": cnt(select(func.count()).where(new_u, U.id.in_(select(D.uid)))),
              "unlocked": cnt(select(func.count()).where(new_u, U.id.in_(unlocked_ids))),
              "exams": cnt(select(func.count()).where(new_u, U.id.in_(mock_ids))),
              "certs": cnt(select(func.count()).where(new_u, U.id.in_(select(certs.c.uid))))}
    dv, ds, da, du = ({k: 0 for k in keys} for _ in range(4))
    for r in c.execute(select((V.ts // DAY).label("d"), func.count()).where(V.ts >= frm, V.li.is_not(True), vsite).group_by("d")):
        k = day_key(int(r[0]) * DAY)
        if k in dv: dv[k] = r[1]
    for r in c.execute(select(func.substr(U.created, 1, 10).label("d"), func.count()).where(new_u).group_by("d")):
        if r[0] in ds: ds[r[0]] = r[1]
    for r in c.execute(select(D.day, func.count()).where(D.day >= first_key_day, D.uid.in_(site_uids)).group_by(D.day)):
        k = day_key(int(r[0]) * DAY)
        if k in da: da[k] = r[1]
    first_unlock = select(E.uid, func.min(E.ts).label("first_ts")).where(E.ev == "unlock").group_by(E.uid).subquery()
    for r in c.execute(select(first_unlock.c.first_ts).where(first_unlock.c.first_ts >= frm, first_unlock.c.uid.in_(site_uids))):
        k = day_key(r[0])
        if k in du: du[k] += 1
    srcs = {}
    Sx = lambda k: srcs.setdefault(k or "direct", {"visits": 0, "signups": 0, "unlocked": 0, "revenue": 0})
    for r in c.execute(select(V.src, func.count()).where(V.ts >= frm, V.li.is_not(True), vsite).group_by(V.src)):
        Sx(r[0] or "direct")["visits"] += r[1]
    for r in c.execute(select(U.src, func.count(), func.sum(case((U.id.in_(unlocked_ids), 1), else_=0))).where(new_u).group_by(U.src)):
        o = Sx(r[0] or "unknown"); o["signups"] += r[1]; o["unlocked"] += int(r[2] or 0)
    for r in c.execute(select(U.src, E.cur, func.sum(E.s)).join(users, users.c.id == E.uid).where(E.ev == "unlock", new_u).group_by(U.src, E.cur)):
        Sx(r[0] or "unknown")["revenue"] += (r[2] or 0) * TO_GBP.get(r[1] or "GBP", 0)
    in_site = S_.uid.in_(site_uids)
    since = lambda t: cnt(select(func.count()).where(S_.last >= t, in_site))
    active = cnt(select(func.count(distinct(D.uid))).where(D.day >= frm_day, D.uid.in_(site_uids)))
    day_rows = cnt(select(func.count()).where(D.day >= frm_day, D.uid.in_(site_uids)))
    mfrm = max(frm, now - 30 * DAY)          # study minutes: one-minute slots with activity (last 30 days at most)
    slots = select(E.uid, (E.ts // 60000).label("m")).where(E.ts >= mfrm, E.ev.in_(ACTIVITY), E.uid.in_(site_uids)).distinct().subquery()
    mins = cnt(select(func.count()).select_from(slots))
    m_active = cnt(select(func.count(distinct(D.uid))).where(D.day >= mfrm // DAY, D.uid.in_(site_uids)))
    engagement = {"dau": since(now - DAY), "wau": since(now - 7 * DAY), "mau": since(now - 30 * DAY), "active": active,
                  "avgDays": round(day_rows / active * 10) / 10 if active else 0, "avgMinutes": round(mins / m_active) if m_active else 0}
    devices = {"visits": {r[0] or "unknown": r[1] for r in c.execute(select(V.dev, func.count()).where(V.ts >= frm, V.li.is_not(True), vsite).group_by(V.dev))},
               "signups": {r[0] or "unknown": r[1] for r in c.execute(select(U.dev, func.count()).where(new_u).group_by(U.dev))}}

    def ret(n):
        pool = and_(U.cday.is_not(None), U.cday <= today - n, usite)
        back = select(D.uid).where(D.uid == U.id, D.day >= U.cday + n).exists()
        return {"pool": cnt(select(func.count()).where(pool)), "back": cnt(select(func.count()).where(pool, back))}

    countries = [[r[0] or "??", r[1]] for r in c.execute(select(U.country, func.count().label("n")).where(new_u).group_by(U.country).order_by(func.count().desc()).limit(15))]
    return {"ok": True, "days": days, "keys": keys, "funnel": funnel,
            "daily": {"visits": [dv[k] for k in keys], "signups": [ds[k] for k in keys], "active": [da[k] for k in keys], "unlocks": [du[k] for k in keys]},
            "sources": sorted([{"src": k, **v} for k, v in srcs.items()], key=lambda x: (-x["signups"], -x["visits"])),
            "engagement": engagement, "devices": devices, "retention": {"d1": ret(1), "d7": ret(7), "d30": ret(30)},
            "totals": {"users": cnt(select(func.count()).where(usite)), "visits": cnt(select(func.count()).where(vsite))},
            "countries": countries}


def ev_out(x):
    n = x.n if x.n is not None else x.cur
    return {"ts": x.ts, "ev": x.ev, "topic": x.topic or "", "qid": out_num(x.qid), "ok": out_num(x.ok), "s": out_num(x.s), "n": out_num(n), "k": x.k or ""}


@admin
def adminStudent(ctx, b):
    c = ctx.c
    r = user_by(c, users.c.id, js_str(b.get("id")))
    if not r: return fail("no_user")
    uid, prog = r["id"], progress(r) or {}
    evs = c.execute(select(events).where(events.c.uid == uid).order_by(events.c.seq.desc()).limit(2000)).all()[::-1]
    ref = user_by(c, users.c.id, r["ref_by"]) if r["ref_by"] else None
    return {"ok": True,
            "profile": {"id": uid, "name": r["name"], "mobile": r["mobile"], "email": r["email"], "university": r["uni"], "course": r["course"],
                        "created": r["created"], "lastLogin": r["last"] or "", "verifiedUni": r["vuni"] or "", "exam": r["exam"] or "",
                        "examUsed": r["exam_used"] or "", "src": r["src"] or "", "dev": r["dev"] or "", "referredBy": ref["name"] if ref else "",
                        "site": site(r["site"]), "country": r["country"] or ""},
            "lessons": len(prog.get("idta-lessons") or []), "mustTicked": len(prog.get("idta-must") or []), "mistakes": len(prog.get("idta-miss") or []),
            "goal": prog.get("idta-goal"), "examDate": prog.get("idta-examdate"),
            "events": [ev_out(x) for x in evs],
            "certs": [{"id": x["id"], "issued": x["issued"], "score": out_num(x["score"]), "n": out_num(x["n"]), "exam": x["exam"]}
                      for x in c.execute(select(certs).where(certs.c.uid == uid).order_by(certs.c.seq)).mappings()],
            "payments": [{"id": x["id"], "at": x["submitted"], "amount": out_num(x["amount"] or 0), "status": x["status"]}
                         for x in c.execute(select(payments).where(payments.c.uid == uid).order_by(payments.c.seq)).mappings()],
            "refunds": [{"id": x["id"], "at": x["requested"], "amount": out_num(x["amount"] or 0), "status": x["status"]}
                        for x in c.execute(select(refunds).where(refunds.c.uid == uid).order_by(refunds.c.seq)).mappings()],
            "referrals": referrals_of(c, uid)}


# ======================================================================
# Exam camera photos (students agree before every exam; deleted after PHOTO_DAYS)
# ======================================================================
def clean_photos(c):
    cut = now_ms() - PHOTO_DAYS * DAY
    old = c.execute(select(proctor.c.id, proctor.c.file).where(proctor.c.ts < cut)).all()
    for r in old:
        store().delete(r.file)
    if old:
        c.execute(delete(proctor).where(proctor.c.ts < cut))
    return len(old)


@action
def proctorShot(ctx, b):
    c = ctx.c
    m = me(ctx, b)
    if not m: return fail("auth")
    data, sess = js_str(b.get("image")), re.sub(r"[^A-Za-z0-9]", "", js_str(b.get("session")))[:16]
    if len(sess) < 6 or len(data) < 200 or len(data) > 400000 or not b64ok(data): return fail("bad_image")
    key = "shot:" + m["id"]
    if too_many(c, key, 300): return fail("too_many_attempts")
    try:
        raw = base64.b64decode(data, validate=True)
    except Exception:
        return fail("bad_image")
    bump(c, key, 86400)
    reason = b.get("reason") if b.get("reason") in ("start", "timer", "tab", "resume", "end") else "timer"
    sid = short_id("S", 9)
    fkey = "photos/" + sid + "-" + sess + "-" + reason + ".jpg"
    store().put(fkey, raw, "image/jpeg")
    t = now_ms()
    c.execute(insert(proctor).values(id=sid, ts=t, time=iso(t), uid=m["id"], name=text(m["name"], 40), session=sess, exam=text(b.get("exam"), 60),
                                     reason=reason, file=fkey))
    return {"ok": True}


@admin
def adminProctor(ctx, b):
    c = ctx.c
    uid, by = js_str(b.get("id")), {}
    for r in c.execute(select(proctor).where(proctor.c.uid == uid).order_by(proctor.c.seq)).mappings():
        o = by.setdefault(r["session"], {"session": r["session"], "exam": r["exam"], "start": r["time"], "shots": [], "tab": 0})
        o["shots"].append({"id": r["id"], "time": r["time"], "reason": r["reason"]})
        if r["reason"] == "tab": o["tab"] += 1
    score = {}
    for r in c.execute(select(events.c.k, events.c.s, events.c.n).where(and_(events.c.uid == uid, events.c.ev == "mock")).order_by(events.c.seq)):
        if r.k:
            score[r.k] = js_str(out_num(r.s)) + "/" + js_str(out_num(r.n))
    return {"ok": True, "days": PHOTO_DAYS, "sessions": [dict(o, score=score.get(k, "")) for k, o in by.items()][::-1]}


@admin
def adminProctorImage(ctx, b):
    r = ctx.c.execute(select(proctor.c.file).where(proctor.c.id == js_str(b.get("id")))).first()
    data = store().get(r.file)[0] if r else None
    if data is None: return fail("no_image")
    return {"ok": True, "image": base64.b64encode(data).decode()}


# ======================================================================
# Capstone projects: students submit, the organiser marks; 50+ issues a certificate
# ======================================================================
@action
def capstoneSubmit(ctx, b):
    c = ctx.c
    m = me(ctx, b, lock=True)
    if not m: return fail("auth")
    title, repo, demo, summary = text(b.get("title"), 80), js_str(b.get("repo")).strip(), js_str(b.get("demo")).strip(), multiline(b.get("summary"), 1500)
    if (len(title) < 4 or not re.fullmatch(r"https://(www\.)?github\.com/[^\s]{3,200}", repo) or (demo and not re.fullmatch(r"https?://\S{4,300}", demo))
            or len(summary) < 100):
        return fail("bad_capstone")
    if c.execute(select(capstones.c.id).where(and_(capstones.c.uid == m["id"], capstones.c.status == "pending"))).first(): return fail("capstone_pending")
    kid = short_id("K", 8)
    c.execute(insert(capstones).values(id=kid, submitted=iso(), uid=m["id"], name=text(m["name"], 40), title=title, repo=text(repo, 220), demo=text(demo, 320),
                                       summary=summary, status="pending", mark=None, feedback="", marked="", cert_id=""))
    ctx.notify("capstone to mark from " + m["name"], m["name"] + ' submitted "' + title + '".\nRepository: ' + repo + ("\nDemo: " + demo if demo else "") +
               "\n\nMark it in Admin -> Capstones.")
    return {"ok": True}


@action
def myCapstone(ctx, b):
    m = me(ctx, b)
    if not m: return fail("auth")
    r = ctx.c.execute(select(capstones).where(capstones.c.uid == m["id"]).order_by(capstones.c.seq.desc()).limit(1)).mappings().first()
    return {"ok": True, "capstone": {"id": r["id"], "submitted": r["submitted"], "title": r["title"], "status": r["status"],
                                     "mark": None if r["mark"] is None else out_num(r["mark"]), "feedback": r["feedback"] or "", "certId": r["cert_id"] or ""} if r else None}


@admin
def adminCapstones(ctx, b):
    lst = [{"id": r["id"], "submitted": r["submitted"], "name": r["name"], "title": r["title"], "repo": r["repo"], "demo": r["demo"] or "",
            "summary": r["summary"], "status": r["status"], "mark": out_num(r["mark"] or 0), "graded": r["marked"] or ""}
           for r in ctx.c.execute(select(capstones).order_by(capstones.c.seq.desc())).mappings()]
    return {"ok": True, "pending": [x for x in lst if x["status"] == "pending"], "recent": [x for x in lst if x["status"] != "pending"][:50]}


@admin
def adminGradeCapstone(ctx, b):
    c = ctx.c
    k = c.execute(select(capstones).where(capstones.c.id == js_str(b.get("id"))).with_for_update()).mappings().first()
    if not k: return fail("no_capstone")
    if k["status"] != "pending": return fail("already_decided")
    n = js_num(b.get("mark"))
    if n is None: return fail("bad_capstone")
    mark, feedback = max(0, min(100, math.floor(n + 0.5))), multiline(b.get("feedback"), 1500)
    band = "Distinction" if mark >= 70 else "Merit" if mark >= 60 else "Pass" if mark >= 50 else ""
    u = user_by(c, users.c.id, k["uid"])
    cert_id = ""
    if band:
        cert_id = new_cert_id(c)
        c.execute(insert(certs).values(id=cert_id, issued=iso(), uid=k["uid"], name=text(k["name"], 40), score=mark, n=100,
                                       exam=text("Capstone · " + band + ": " + k["title"], 60), site=site(u["site"]) if u else "global"))
    if not c.execute(update(capstones).where(and_(capstones.c.id == k["id"], capstones.c.status == "pending"))
                     .values(status="graded", mark=mark, feedback=feedback, marked=iso(), cert_id=cert_id)).rowcount:
        raise RuntimeError("capstone marked twice")     # rolls back the certificate too
    if u:
        ctx.mail(u["email"], brand(u["site"]) + ": your capstone has been marked",
                 "Hi " + u["name"] + ',\n\nYour capstone "' + k["title"] + '" scored ' + str(mark) + "/100" +
                 (" (" + band + "). Your certificate is in the Career hub." if band else ". Use the feedback to improve it and submit again.") +
                 ("\n\nFeedback:\n" + feedback if feedback else ""))
    return {"ok": True, "certId": cert_id}


# ======================================================================
# Leaderboard & admin overview
# ======================================================================
def blank():
    return {"last": 0, "q": 0, "qr": 0, "mocks": 0, "best": None, "lastMock": "", "lab": 0, "labr": 0, "today": 0}


def stat_row(r):
    """Per-student totals from the user_stats summary (None when the student hasn't done anything yet)."""
    if r is None or r.get("uid") is None:
        return blank()
    q = int(r["q"] or 0)
    return {"last": r["last"] or 0, "q": q, "qr": int(r["qr"] or 0), "mocks": int(r["mocks"] or 0), "best": out_num(r["best"]),
            "lastMock": r["last_mock"] or "", "lab": int(r["lab"] or 0), "labr": int(r["labr"] or 0), "today": 0}


def topic_totals(c):
    return {r.topic: [int(r.correct), int(r.total)] for r in c.execute(select(topic_stats))}


def event_rows(c):
    """Exact count for small tables, the planner's estimate once there are millions."""
    if c.dialect.name == "postgresql":
        est = c.exec_driver_sql("SELECT reltuples::bigint FROM pg_class WHERE relname = 'events'").scalar() or 0
        if est > 500_000:
            return int(est)
    return c.execute(select(func.count()).select_from(events)).scalar()


LB_LIMIT = 300


@action
def leaderboard(ctx, b):
    """Top students (best full mock, then accuracy, then answers). Optionally for one university, plus your own row."""
    c = ctx.c
    uni = text(b.get("uni"), 100)
    key = "leaderboard:" + uni
    out = cache.get(key)
    if not out:
        S_ = user_stats.c
        acc = case((S_.q > 0, S_.qr * 1.0 / S_.q), else_=None)
        q = (select(users.c.id, users.c.name, users.c.course, users.c.vuni, users.c.uni, user_stats).select_from(users.outerjoin(user_stats, S_.uid == users.c.id))
             .where(users.c.lb.is_(True)).order_by(S_.best.desc().nulls_last(), acc.desc().nulls_last(), func.coalesce(S_.q, 0).desc(), users.c.seq).limit(LB_LIMIT))
        if uni:
            q = q.where(or_(users.c.vuni == uni, users.c.uni == uni))
        rows = []
        for r in c.execute(q).mappings():
            st = stat_row(r)
            rows.append({"pid": pid(r["id"]), "name": r["name"], "course": r["course"], "university": r["vuni"] or r["uni"] or "", "verified": bool(r["vuni"]),
                         "best": st["best"], "mocks": st["mocks"], "q": st["q"], "acc": round(st["qr"] / st["q"] * 100) if st["q"] else None})
        out = {"ok": True, "players": c.execute(select(func.count()).select_from(users)).scalar(),
               "onBoard": c.execute(select(func.count()).select_from(users).where(users.c.lb.is_(True))).scalar(),
               "topics": topic_totals(c), "leaderboard": rows, "limit": LB_LIMIT}
        cache.put(key, out, 60)
    uid = auth(c, b.get("token")) if b.get("token") else None
    if uid and not any(r["pid"] == pid(uid) for r in out["leaderboard"]):
        r = c.execute(select(users.c.id, users.c.name, users.c.course, users.c.vuni, users.c.uni, users.c.lb, user_stats)
                      .select_from(users.outerjoin(user_stats, user_stats.c.uid == users.c.id)).where(users.c.id == uid)).mappings().first()
        if r and r["lb"]:
            st = stat_row(r)
            return {**out, "me": {"pid": pid(uid), "name": r["name"], "course": r["course"], "university": r["vuni"] or r["uni"] or "", "verified": bool(r["vuni"]),
                                  "best": st["best"], "mocks": st["mocks"], "q": st["q"], "acc": round(st["qr"] / st["q"] * 100) if st["q"] else None}}
    return out


ADMIN_LIST = 1000


def admin_users(c, where=None, order=None, limit=ADMIN_LIST, offset=0):
    q = select(users, user_stats.c.uid, user_stats.c.last.label("s_last"), user_stats.c.q, user_stats.c.qr, user_stats.c.mocks, user_stats.c.best,
               user_stats.c.last_mock, user_stats.c.lab, user_stats.c.labr).select_from(users.outerjoin(user_stats, user_stats.c.uid == users.c.id))
    if where is not None:
        q = q.where(where)
    q = q.order_by(*(order or [func.coalesce(user_stats.c.last, 0).desc(), users.c.seq.desc()])).limit(limit).offset(offset)
    rows = c.execute(q).mappings().all()
    day_ago = now_ms() - DAY
    today = {}
    ids = [r["id"] for r in rows]
    for i in range(0, len(ids), 500):
        for r in c.execute(select(events.c.uid, func.count()).where(events.c.uid.in_(ids[i:i + 500]), events.c.ts > day_ago).group_by(events.c.uid)):
            today[r[0]] = r[1]
    out = []
    for r in rows:
        st = stat_row({**r, "last": r["s_last"]})
        st["today"] = today.get(r["id"], 0)
        out.append({"id": r["id"], "name": r["name"], "mobile": r["mobile"], "email": r["email"], "university": r["uni"], "course": r["course"],
                    "lb": bool(r["lb"]), "created": r["created"], "lastLogin": r["last"] or "", "uniEmail": r["uni_email"] or "",
                    "verifiedUni": r["vuni"] or "", "examAccess": r["exam"] or "", "site": site(r["site"]), "country": r["country"] or "", **st})
    return out


@admin
def admin_(ctx, b):
    """Admin overview. Lists the most recently active students (or search results) with class-wide totals."""
    c = ctx.c
    S_ = user_stats.c
    qtext = text(b.get("q"), 80).lower()
    where = None
    if qtext:
        like = "%" + qtext.replace("%", "").replace("_", "") + "%"
        where = or_(*[func.lower(col).like(like) for col in (users.c.name, users.c.mobile, users.c.email, users.c.uni, users.c.course, users.c.vuni)])
    us = admin_users(c, where, limit=500 if qtext else ADMIN_LIST)
    now = now_ms()
    agg = c.execute(select(func.coalesce(func.sum(S_.q), 0), func.coalesce(func.sum(S_.qr), 0), func.coalesce(func.sum(S_.mocks), 0),
                           func.sum(case((S_.last > now - DAY, 1), else_=0)), func.sum(case((S_.last > now - 7 * DAY, 1), else_=0)))).first()
    totals = {"accounts": c.execute(select(func.count()).select_from(users)).scalar(),
              "onBoard": c.execute(select(func.count()).select_from(users).where(users.c.lb.is_(True))).scalar(),
              "q": int(agg[0]), "qr": int(agg[1]), "mocks": int(agg[2]), "active24h": int(agg[3] or 0), "active7d": int(agg[4] or 0), "shown": len(us)}
    courses = [[r[0] or "–", r[1]] for r in c.execute(select(users.c.course, func.count().label("n")).group_by(users.c.course).order_by(func.count().desc()).limit(30))]
    rec = c.execute(select(events).order_by(events.c.seq.desc()).limit(200)).all()
    recent = [{"ts": x.ts, "name": x.name, "ev": x.ev, "topic": x.topic or "", "qid": out_num(x.qid), "ok": out_num(x.ok), "s": out_num(x.s),
               "n": out_num(x.n if x.n is not None else x.cur), "k": x.k or ""} for x in rec]
    return {"ok": True, "rows": event_rows(c), "topics": topic_totals(c),
            "questions": {r.qid: [int(r.correct), int(r.total)] for r in c.execute(select(question_stats))},
            "recent": recent, "users": us, "totals": totals, "courses": courses, "search": qtext}


@admin
def adminUsersPage(ctx, b):
    """All students, page by page (for the CSV export)."""
    off = max(0, int(js_num(b.get("offset")) or 0))
    lim = max(1, min(5000, int(js_num(b.get("limit")) or 2000)))
    us = admin_users(ctx.c, None, order=[users.c.seq], limit=lim, offset=off)
    return {"ok": True, "users": us, "next": off + len(us) if len(us) == lim else None}


# ---------- housekeeping (runs at most hourly, triggered by traffic) ----------
def housekeeping(c):
    clean_photos(c)
    from .db import counters
    now = now_ms()
    c.execute(delete(sessions).where(sessions.c.expires < now))
    c.execute(delete(counters).where(counters.c.exp < now))
