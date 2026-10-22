"""Copy everything from the old Google Apps Script backend into the new database.

    DATABASE_URL=... GCS_BUCKET=... ADMIN_KEY=... python migrate.py --from https://script.google.com/macros/s/XXX/exec --wipe

Students keep their accounts, passwords, progress, purchases and certificates. Run it once to try, and once more
(with --wipe) right before switching the website over. It is safe to run again: --wipe empties the new tables first.
"""
import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import delete, insert  # noqa: E402

from app import db  # noqa: E402
from app.api import set_setting  # noqa: E402
from app.services import store  # noqa: E402
from app.util import iso, parse_ms, ref_code, ref_norm, site  # noqa: E402


def call(url, body, tries=4):
    data = json.dumps(body).encode()
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "text/plain"})
            with urllib.request.urlopen(req, timeout=120) as r:
                out = json.loads(r.read().decode())
            if out.get("error") == "busy" and i < tries - 1:
                time.sleep(3)
                continue
            return out
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(3 * (i + 1))


def s(v):
    """Cell to text, dropping the apostrophe the old backend added to stop spreadsheet formulas."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    t = str(v)
    return t[1:] if t.startswith("'") else t


def num(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def ms(v):
    return parse_ms(v) if v not in (None, "") else None


def isos(v):
    m = ms(v)
    return iso(m) if m is not None else s(v)


def rows_of(url, key, sheet, page):
    out, off = [], 0
    while True:
        r = call(url, {"action": "adminExport", "key": key, "sheet": sheet, "offset": off, "limit": page})
        if not r.get("ok"):
            raise SystemExit(f"export of {sheet} failed: {r}")
        out += r["rows"]
        off += len(r["rows"])
        print(f"  {sheet}: {off}/{r['total']}", end="\r")
        if not r["rows"] or off >= r["total"]:
            print()
            return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", required=True, help="your Apps Script /exec URL")
    ap.add_argument("--key", default=os.environ.get("ADMIN_KEY", ""), help="admin key (or set ADMIN_KEY)")
    ap.add_argument("--wipe", action="store_true", help="empty the new database first")
    ap.add_argument("--skip-files", action="store_true", help="don't copy screenshots and exam photos")
    a = ap.parse_args()
    if not a.key:
        raise SystemExit("Set ADMIN_KEY or pass --key")
    url, key = a.src, a.key
    db.init_db()
    eng = db.engine
    props = call(url, {"action": "adminExport", "key": key, "sheet": "props"})
    if not props.get("ok"):
        raise SystemExit(f"Could not read from the old backend: {props}. Is the new Code.gs deployed and the key right?")
    data = {k: rows_of(url, key, k, 100 if k == "users" else 2000) for k in
            ["users", "sessions", "events", "news", "questions", "coupons", "certs", "payments", "refunds", "payouts", "visits", "capstones", "proctor"]}

    with eng.begin() as c:
        if a.wipe:
            for t in reversed(db.md.sorted_tables):
                c.execute(delete(t))
        for k, v in props["props"].items():
            if v:
                set_setting(c, k, v)

        U = data["users"]
        c.execute(insert(db.users), [dict(
            id=s(r[0]), created=isos(r[1]), name=s(r[2]), mobile=s(r[3]).lstrip("+"), email=s(r[4]).lower(), uni=s(r[5]), course=s(r[6]), hash=s(r[7]), salt=s(r[8]),
            lb=s(r[9]) != "no", prog=s(r[10]), prog_at=isos(r[11]), last=isos(r[12]), rcode=s(r[13]), rexp=isos(r[14]), consent=isos(r[15]),
            uni_email=s(r[16]) if len(r) > 16 else "", vuni=s(r[17]) if len(r) > 17 else "", vcode=s(r[18]) if len(r) > 18 else "",
            vexp=isos(r[19]) if len(r) > 19 else "", exam=s(r[20]) if len(r) > 20 else "", exam_used=isos(r[21]) if len(r) > 21 else "",
            ref_by=s(r[22]) if len(r) > 22 else "", ref_code=ref_code(s(r[0])), src=s(r[23]) if len(r) > 23 else "", dev=s(r[24]) if len(r) > 24 else "",
            site=site(s(r[25])) if len(r) > 25 else "uk", country=s(r[26]) if len(r) > 26 else "") for r in U if s(r[0])] or [])
        now = int(time.time() * 1000)
        ses = [dict(token_hash=s(r[0]), uid=s(r[1]), created=isos(r[2]), expires=ms(r[3]) or 0) for r in data["sessions"] if s(r[0]) and (ms(r[3]) or 0) > now]
        if ses:
            c.execute(insert(db.sessions), ses)

        evs = []
        for r in data["events"]:
            if not s(r[1]) or not s(r[3]):
                continue
            ev = s(r[3])
            sale = ev in ("unlock", "revoke", "refund", "payout", "restore")
            evs.append(dict(ts=ms(r[0]) or now, uid=s(r[1]), name=s(r[2]), ev=ev, topic=s(r[4]) or None, qid=num(r[5]), ok=None if r[6] in ("", None) else int(num(r[6]) or 0),
                            s=num(r[7]), n=None if sale else num(r[8]), cur=(s(r[8]) or None) if sale else None, k=s(r[9]) or None))
        for i in range(0, len(evs), 5000):
            c.execute(insert(db.events), evs[i:i + 5000])

        def put(table, rows):
            if rows:
                c.execute(insert(table), rows)

        put(db.news, [dict(id=s(r[0]), created=isos(r[1]), title=s(r[2]), message=s(r[3]), level=s(r[4]) or "info", expires=isos(r[5]), audience=s(r[6]))
                      for r in data["news"] if s(r[0])])
        put(db.questions, [dict(id=int(num(r[0])), updated=isos(r[1]), t=s(r[2]), type=s(r[3]), p=s(r[4]), o=s(r[5]), a=s(r[6]), e=s(r[7]), audience=s(r[8]))
                           for r in data["questions"] if num(r[0])])
        put(db.coupons, [dict(code=s(r[0]), created=isos(r[1]), max=int(num(r[2]) or 1), used=int(num(r[3]) or 0), expires=isos(r[4]), note=s(r[5]),
                              used_by=s(r[6]), value=num(r[7]) or 0) for r in data["coupons"] if s(r[0])])
        put(db.certs, [dict(id=s(r[0]), issued=isos(r[1]), uid=s(r[2]), name=s(r[3]), score=num(r[4]), n=num(r[5]), exam=s(r[6]), site=site(s(r[7])))
                       for r in data["certs"] if s(r[0])])
        pays = []
        for r in data["payments"]:
            if not s(r[0]):
                continue
            pid_, fid, h, fkey = s(r[0]), s(r[7]), None, ""
            if fid and not a.skip_files:
                img = call(url, {"action": "adminPaymentImage", "key": key, "id": pid_})
                if img.get("ok"):
                    raw = base64.b64decode(img["image"])
                    mime = img.get("mime") or "image/jpeg"
                    fkey = "payments/" + pid_ + {"image/png": ".png", "image/webp": ".webp"}.get(mime, ".jpg")
                    store().put(fkey, raw, mime)
                    h = hashlib.sha256(raw).hexdigest()
            pays.append(dict(id=pid_, submitted=isos(r[1]), uid=s(r[2]), name=s(r[3]), mobile=s(r[4]), amount=num(r[5]) or 0, ref=s(r[6]), ref_norm=ref_norm(s(r[6])),
                             file=fkey, img_hash=h, status=s(r[8]), decided=isos(r[9]), note=s(r[10]), cur=s(r[11]) if len(r) > 11 else "GBP", flags="", ip=""))
        put(db.payments, pays)
        put(db.refunds, [dict(id=s(r[0]), requested=isos(r[1]), uid=s(r[2]), name=s(r[3]), mobile=s(r[4]), amount=num(r[5]) or 0, to=s(r[6]), reason=s(r[7]),
                              status=s(r[8]), decided=isos(r[9]), note=s(r[10]), cur=s(r[11]) if len(r) > 11 else "GBP") for r in data["refunds"] if s(r[0])])
        put(db.payouts, [dict(id=s(r[0]), requested=isos(r[1]), uid=s(r[2]), name=s(r[3]), mobile=s(r[4]), amount=num(r[5]) or 0, to=s(r[6]), status=s(r[7]),
                              decided=isos(r[8]), note=s(r[9])) for r in data["payouts"] if s(r[0])])
        put(db.visits, [dict(ts=ms(r[0]) or now, src=s(r[1]) or "direct", dev=s(r[2]), li=s(r[3]) == "yes", ret=s(r[4]) == "yes", site=site(s(r[5])))
                        for r in data["visits"] if r[0] not in ("", None)])
        put(db.capstones, [dict(id=s(r[0]), submitted=isos(r[1]), uid=s(r[2]), name=s(r[3]), title=s(r[4]), repo=s(r[5]), demo=s(r[6]), summary=s(r[7]),
                                status=s(r[8]), mark=num(r[9]), feedback=s(r[10]), marked=isos(r[11]), cert_id=s(r[12])) for r in data["capstones"] if s(r[0])])
        shots = []
        for r in data["proctor"]:
            if not s(r[0]):
                continue
            fkey = ""
            if not a.skip_files:
                img = call(url, {"action": "adminProctorImage", "key": key, "id": s(r[0])})
                if img.get("ok"):
                    fkey = "photos/" + s(r[0]) + "-" + s(r[4]) + "-" + s(r[6]) + ".jpg"
                    store().put(fkey, base64.b64decode(img["image"]), "image/jpeg")
            shots.append(dict(id=s(r[0]), ts=ms(r[1]) or now, time=isos(r[1]), uid=s(r[2]), name=s(r[3]), session=s(r[4]), exam=s(r[5]), reason=s(r[6]), file=fkey))
        put(db.proctor, shots)

    print("Copied: " + ", ".join(f"{k} {len(v)}" for k, v in data.items()))
    print("Done. Passwords, sessions and purchases carried over; students stay logged in.")


if __name__ == "__main__":
    main()
