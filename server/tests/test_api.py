"""API tests. Run: cd server && pytest -q   (uses SQLite; set TEST_DATABASE_URL=postgresql://... to run on Postgres)."""
import base64
import hashlib
import io
import os
import tempfile
import threading

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_tmp}/t.db"
os.environ.update(ADMIN_KEY="k-test", MAIL_MODE="test", FILES_DIR=f"{_tmp}/files", NOTIFY_EMAIL="owner@example.com")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app import api, db, main  # noqa: E402
from app.services import cache, mailer  # noqa: E402

client = TestClient(main.app)
PW = hashlib.sha256(b"browser-derived").hexdigest()


def call(action, **kw):
    r = client.post("/", content=__import__("json").dumps({"action": action, **kw}), headers={"Content-Type": "text/plain;charset=UTF-8"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "*"
    return r.json()


@pytest.fixture(autouse=True)
def fresh():
    db.md.drop_all(db.engine)
    db.md.create_all(db.engine)
    cache.clear()
    api._pepper = None
    mailer.sent.clear()
    yield


n = [0]


def user(name="Asha Rao", country="IN", **kw):
    n[0] += 1
    r = call("signup", name=name, mobile=f"0770090{n[0]:04d}", email=f"u{n[0]}@example.com", university="Uni", course="MSc",
             pw=PW, consent=True, country=country, **kw)
    assert r["ok"], r
    return r


def png(text="paid", size=(400, 700)):
    im = Image.new("RGB", size, "white")
    from PIL import ImageDraw
    ImageDraw.Draw(im).text((20, 40), text, fill="black")
    b = io.BytesIO()
    im.save(b, "PNG")
    return base64.b64encode(b.getvalue()).decode()


def test_password_hash_matches_apps_script():
    # Same algorithm as hashPw_ in Code.gs: 50 rounds of SHA-256 over "salt|derived|pepper"
    with db.engine.begin() as c:
        api.set_setting(c, "PEPPER", "pep")
        api._pepper = None
        h = "salt|abc|pep"
        for _ in range(50):
            h = hashlib.sha256(h.encode()).hexdigest()
        assert api.hash_pw(c, "abc", "salt") == h


def test_signup_login_progress_and_leaderboard():
    u = user()
    tok = u["token"]
    assert u["profile"]["country"] == "IN" and u["profile"]["refCode"] == u["profile"]["pid"][:8].upper()
    assert call("signup", name="X Y", mobile="+44 7700 900001", email="u1@example.com", university="Uni", course="MSc", pw=PW, consent=True)["error"] == "email_taken"
    assert call("login", mobile=u["profile"]["mobile"], pw="0" * 64)["error"] == "wrong_login"
    r = call("login", mobile=u["profile"]["mobile"], pw=PW)
    assert r["ok"] and r["profile"]["name"] == "Asha Rao"
    assert call("saveProgress", token=tok, progress={"idta-lessons": ["l1"]})["ok"]
    assert call("me", token=tok)["progress"] == {"idta-lessons": ["l1"]}
    evs = [{"t": "q", "topic": "eval", "id": 5, "ok": True}, {"t": "q", "topic": "eval", "id": 6, "ok": False},
           {"t": "mock", "s": 25, "n": 30, "k": "abc123"}, {"t": "unlock", "s": 999}]          # server-only types are ignored
    assert call("events", token=tok, events=evs)["saved"] == 3
    lb = call("leaderboard")
    row = lb["leaderboard"][0]
    assert (row["best"], row["mocks"], row["q"], row["acc"]) == (25, 1, 2, 50) and lb["topics"]["eval"] == [1, 2]
    a = call("admin", key="k-test")
    assert a["rows"] == 3 and a["users"][0]["q"] == 2 and a["questions"]["5"] == [1, 1]
    assert call("admin", key="nope")["error"] == "bad_key"
    assert client.get("/?action=leaderboard").json()["ok"]


def test_login_lockout_and_reset_code():
    u = user()
    mob = u["profile"]["mobile"]
    for _ in range(5):
        call("login", mobile=mob, pw="1" * 64)
    assert call("login", mobile=mob, pw=PW)["error"] == "too_many_attempts"
    assert call("forgot", mobile=mob)["ok"]
    code = mailer.sent[-1]["body"].split("code is: ")[1][:6]
    r = call("reset", mobile=mob, code=code, pw="2" * 64)
    assert r["ok"]
    assert call("login", mobile=mob, pw="2" * 64)["ok"]          # reset also clears the lockout
    assert call("me", token=u["token"])["error"] == "auth"       # old sessions are signed out


def test_payment_flow_duplicates_and_admin_approval():
    a, b = user("Neha Gupta"), user("Omar Ali", country="GB")
    img = png("UTR 412345678901")
    assert call("paySubmit", token=a["token"], mime="image/png", image=img, ref="")["error"] == "bad_ref"
    assert call("paySubmit", token=a["token"], mime="image/png", image=base64.b64encode(b"not an image" * 30).decode(), ref="412345678901")["error"] == "bad_image"
    r = call("paySubmit", token=a["token"], mime="image/png", image=img, ref="4123 4567 8901")
    assert r["ok"] and r["payment"]["status"] == "pending"
    assert any("payment to check" in m["subject"] for m in mailer.sent)
    assert call("paySubmit", token=a["token"], mime="image/png", image=img, ref="x" * 8)["error"] == "pay_pending"
    # Omar reuses the same reference before it's approved: allowed but flagged
    r2 = call("paySubmit", token=b["token"], mime="image/png", image=png("other"), ref="412345678901")
    assert r2["ok"]
    pm = call("adminPayments", key="k-test")
    flagged = [p for p in pm["pending"] if p["name"] == "Omar Ali"][0]
    assert flagged["flags"] and "reference" in flagged["flags"][0]
    assert call("adminPaymentImage", key="k-test", id=r["payment"]["id"])["mime"] == "image/png"
    assert call("adminDecidePayment", key="k-test", id=r["payment"]["id"], approve=True)["ok"]
    assert call("adminDecidePayment", key="k-test", id=r["payment"]["id"], approve=True)["error"] == "already_decided"
    assert call("payStatus", token=a["token"])["profile"]["examAccess"]
    sales = call("adminSales", key="k-test")
    assert sales["sales"][0]["orig"] == 199 and sales["sales"][0]["cur"] == "INR"
    # after rejection, Omar can't reuse Neha's approved reference or screenshot
    call("adminDecidePayment", key="k-test", id=r2["payment"]["id"], approve=False, note="wrong amount")
    assert call("paySubmit", token=b["token"], mime="image/png", image=png("new"), ref="412345678901")["error"] == "duplicate_payment"
    assert call("paySubmit", token=b["token"], mime="image/png", image=img, ref="999988887777")["error"] == "duplicate_payment"
    assert any("blocked duplicate" in m["subject"] for m in mailer.sent)
    assert call("paySubmit", token=b["token"], mime="image/png", image=png("fresh"), ref="999988887777")["ok"]


def test_refund_only_before_first_exam():
    u = user()
    call("adminSetExam", key="k-test", id=u["profile"]["id"], on=True, amount="region")
    p = call("myPurchase", token=u["token"])
    assert p["refundable"] and p["paid"] == 199
    call("examStarted", token=u["token"])
    assert call("refundRequest", token=u["token"], to="me@upi")["error"] == "exam_used"


def test_coupon_cannot_be_overused_concurrently():
    call("adminCreateCoupon", key="k-test", code="ONCE-ONLY", uses=1, value=3)
    toks = [user()["token"] for _ in range(8)]
    res = []
    th = [threading.Thread(target=lambda t=t: res.append(call("redeem", token=t, code="ONCE-ONLY"))) for t in toks]
    [t.start() for t in th]
    [t.join() for t in th]
    assert len([r for r in res if r.get("ok")]) == 1, res


def test_referral_reward_after_paid_and_ten_days():
    a = user()
    b = user(ref=a["profile"]["refCode"])
    call("adminSetExam", key="k-test", id=b["profile"]["id"], on=True, amount="region")
    day = 86_400_000
    t0 = api.now_ms() - 20 * day
    call("events", token=b["token"], events=[{"t": "q", "ts": t0 + i * day, "topic": "data", "id": 1, "ok": True} for i in range(10)])
    r = call("myReferrals", token=a["token"])
    assert r["qualified"] == 1 and r["available"] == 1
    assert call("referralPayout", token=a["token"], to="a@upi")["ok"]
    assert call("myReferrals", token=a["token"])["available"] == 0


def test_proctor_photos_and_account_delete():
    u = user()
    jpg = io.BytesIO()
    Image.new("RGB", (160, 120)).save(jpg, "JPEG")
    shot = base64.b64encode(jpg.getvalue()).decode() + "=" * 0
    assert call("proctorShot", token=u["token"], image=shot, session="abc123", reason="start", exam="Paper 1")["ok"]
    s = call("adminProctor", key="k-test", id=u["profile"]["id"])
    sid = s["sessions"][0]["shots"][0]["id"]
    assert call("adminProctorImage", key="k-test", id=sid)["ok"]
    assert call("deleteAccount", token=u["token"], pw=PW)["ok"]
    assert call("adminProctorImage", key="k-test", id=sid)["error"] == "no_image"


def test_capstone_marking_issues_certificate():
    u = user()
    assert call("capstoneSubmit", token=u["token"], title="Churn model", repo="https://github.com/a/b", summary="x" * 120)["ok"]
    k = call("adminCapstones", key="k-test")["pending"][0]
    r = call("adminGradeCapstone", key="k-test", id=k["id"], mark=72, feedback="Great")
    assert r["certId"].startswith("C")
    c = call("cert", id=r["certId"])["cert"]
    assert c["score"] == 72 and "Distinction" in c["exam"]


def test_prices_and_config():
    regs = call("config")["exam"]["regions"]
    regs[1]["how"] = "UPI: someone@upi\nBank transfer details"
    assert call("adminSetPrices", key="k-test", regions=regs)["ok"]
    assert "UPI: someone@upi" in call("config")["exam"]["regions"][1]["how"]
    assert call("adminSetPrices", key="k-test", regions=[{"id": "GB"}])["error"] == "bad_prices"


def test_body_limit_and_bad_json():
    assert client.post("/", content=b"{nope").json()["error"] == "bad_request"
    assert client.post("/", content=b"x" * 4_100_000).status_code == 413
    assert call("nosuch")["error"] == "unknown_action"
