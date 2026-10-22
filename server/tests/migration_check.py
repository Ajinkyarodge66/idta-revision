"""Migration + parity check against the real Code.gs (run inside the Node stub that emulates Apps Script).
1) creates realistic data in the old backend, 2) runs migrate.py, 3) compares every read action old vs new."""
import base64, hashlib, io, json, os, subprocess, sys, tempfile, time, urllib.request
from PIL import Image

STUB = "http://localhost:8767/exec"
tmp = tempfile.mkdtemp()
os.environ.update(DATABASE_URL=f"sqlite:///{tmp}/m.db", ADMIN_KEY="secret123", MAIL_MODE="test", FILES_DIR=f"{tmp}/files")
K = "secret123"


def old(**b):
    req = urllib.request.Request(STUB, data=json.dumps(b).encode(), headers={"Content-Type": "text/plain"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def png(t):
    im = Image.new("RGB", (300, 500), "white"); b = io.BytesIO(); im.putpixel((t, t), (0, 0, 0)); im.save(b, "PNG")
    return base64.b64encode(b.getvalue()).decode()


PW = hashlib.sha256(b"pw").hexdigest()
users = []
for i, (name, ctry) in enumerate([("Asha Rao", "IN"), ("Ben Cole", "GB"), ("Chen Li", "US"), ("Dana Ivy", "")]):
    r = old(action="signup", name=name, mobile=f"0770090{i:04d}", email=f"m{i}@example.com", university="University of Portsmouth", course="MSc",
            pw=PW, consent=True, country=ctry, ref=users[0]["profile"]["refCode"] if users else "", src="whatsapp", dev="phone")
    assert r["ok"], r
    users.append(r)
A, B, C, D = [u["token"] for u in users]
day = 86400000
now = int(time.time() * 1000)
for t, n in ((A, 40), (B, 25), (C, 5)):
    old(action="events", token=t, events=[{"t": "q", "ts": now - (j % 12) * day, "topic": ["eval", "nlp", "cls"][j % 3], "id": j % 50, "ok": j % 3 > 0} for j in range(n)]
        + [{"t": "mock", "ts": now - day, "s": 20 + (n % 7), "n": 30, "k": "sess" + str(n)}, {"t": "lab", "ok": True}])
old(action="saveProgress", token=A, progress={"idta-lessons": ["l1", "l2"], "idta-goal": 20})
old(action="adminSetPrices", key=K, regions=old(action="config")["exam"]["regions"])
old(action="adminCreateCoupon", key=K, code="FREE-ONE", uses=3, value=3, note="=SUM(1)")
assert old(action="redeem", token=C, code="FREE-ONE")["ok"]
p = old(action="paySubmit", token=B, mime="image/png", image=png(5), ref="412345678901")
assert p["ok"], p
old(action="adminDecidePayment", key=K, id=p["payment"]["id"], approve=True)
p2 = old(action="paySubmit", token=A, mime="image/png", image=png(9), ref="UTR 9")
assert old(action="adminDecidePayment", key=K, id=p2["payment"]["id"], approve=True)["ok"]
assert old(action="refundRequest", token=A, to="asha@upi", reason="changed mind")["ok"]
old(action="certIssue", token=B, n=30, score=26, exam="Paper 1")
old(action="capstoneSubmit", token=B, title="Fraud model", repo="https://github.com/b/fraud", summary="y" * 150)
jpg = io.BytesIO(); Image.new("RGB", (160, 120)).save(jpg, "JPEG")
old(action="proctorShot", token=B, image=base64.b64encode(jpg.getvalue()).decode(), session="abcdef12", reason="start", exam="Paper 1")
old(action="adminAnnounce", key=K, title="Exam on Friday", message="Room 2", days=5)
old(action="adminAnnounce", key=K, title="Portsmouth only", message="secret", days=5, audience="University of Portsmouth")
old(action="adminSaveQuestion", key=K, question={"t": "eval", "type": "single", "p": "What is recall?", "o": ["TP/(TP+FN)", "TP/(TP+FP)"], "a": 0, "e": "def"})
old(action="adminSetAccess", key=K, id=users[1]["profile"]["id"], uni="University of Portsmouth")
for _ in range(3):
    old(action="hit", src="google", dev="desktop", site="global")

# --- migrate ---
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
out = subprocess.run([sys.executable, os.path.join(here, "migrate.py"), "--from", STUB, "--key", K, "--wipe"], capture_output=True, text=True, env=os.environ)
print(out.stdout[-600:], out.stderr[-2000:])
assert out.returncode == 0

sys.path.insert(0, here)
from fastapi.testclient import TestClient  # noqa: E402
from app import main  # noqa: E402
cl = TestClient(main.app)


def new(**b):
    return cl.post("/", content=json.dumps(b)).json()


def norm(o, drop=()):
    if isinstance(o, dict):
        return {k: norm(v, drop) for k, v in o.items() if k not in drop}
    if isinstance(o, list):
        return [norm(x, drop) for x in o]
    if isinstance(o, float) and o.is_integer():
        return int(o)
    return o


checks = 0


def same(name, a, b, drop=()):
    global checks
    a, b = norm(a, drop), norm(b, drop)
    if a != b:
        def first(x, y, path=""):
            if type(x) != type(y): return path + f" {x!r} != {y!r}"
            if isinstance(x, dict):
                for k in sorted(set(x) | set(y)):
                    if x.get(k, "<missing>") != y.get(k, "<missing>"): return first(x.get(k, "<missing>"), y.get(k, "<missing>"), path + "." + k)
            if isinstance(x, list):
                if len(x) != len(y): return path + f" len {len(x)} != {len(y)}"
                for i, (p_, q_) in enumerate(zip(x, y)):
                    if p_ != q_: return first(p_, q_, path + f"[{i}]")
            return path + f" {x!r} != {y!r}"
        print("MISMATCH", name, first(a, b))
        print(" old:", json.dumps(a, sort_keys=True)[:1500])
        print(" new:", json.dumps(b, sort_keys=True)[:1500])
        raise SystemExit(1)
    checks += 1
    print("same:", name)


# students stay logged in, and passwords still work
for u, t in zip(users, (A, B, C, D)):
    same("me " + u["profile"]["name"], old(action="me", token=t), new(action="me", token=t))
    r = new(action="login", mobile=u["profile"]["mobile"], pw=PW)
    assert r["ok"], r
for t in (A, B, C):
    for act in ("myPurchase", "payStatus", "myReferrals", "myCerts", "myCapstone", "pack"):
        same(act, old(action=act, token=t), new(action=act, token=t))
same("config", old(action="config"), new(action="config"), drop=("at",))
same("leaderboard", old(action="leaderboard"), new(action="leaderboard"))
for act in ("adminSales", "adminPayments", "adminRefunds", "adminReferrals", "adminCoupons", "adminCapstones", "adminContent"):
    same(act, old(action=act, key=K), new(action=act, key=K), drop=("flags",))
same("adminProctor", old(action="adminProctor", key=K, id=users[1]["profile"]["id"]), new(action="adminProctor", key=K, id=users[1]["profile"]["id"]))
same("adminStudent", old(action="adminStudent", key=K, id=users[0]["profile"]["id"]), new(action="adminStudent", key=K, id=users[0]["profile"]["id"]), drop=("lastLogin",))
oa, na = old(action="admin", key=K), new(action="admin", key=K)
same("admin overview", oa, na, drop=("recent", "lastLogin"))
same("admin recent", oa["recent"][:50], na["recent"][:50], drop=("ts",))
same("analytics", old(action="adminAnalytics", key=K, days=30), new(action="adminAnalytics", key=K, days=30))
pid = new(action="adminPayments", key=K)["recent"][0]["id"]
assert new(action="adminPaymentImage", key=K, id=pid)["image"] == old(action="adminPaymentImage", key=K, id=pid)["image"]
print(f"OK: {checks} read actions identical after migration; screenshots copied; passwords work")
