"""Simulate many students using the app at once.  python loadtest.py http://localhost:8080 300 60
(base URL, simultaneous students, seconds). Each student signs up, then keeps answering questions, saving progress
and opening the leaderboard, like real use but much faster."""
import hashlib, json, random, statistics, sys, threading, time, urllib.request

BASE, N, SECS = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
lat, errs, lock = [], [], threading.Lock()


def post(b):
    t = time.time()
    try:
        req = urllib.request.Request(BASE, data=json.dumps(b).encode(), headers={"Content-Type": "text/plain"})
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.loads(r.read())
    except Exception as e:
        out = {"ok": False, "error": type(e).__name__}
    with lock:
        lat.append(time.time() - t)
        if not out.get("ok"):
            errs.append(out.get("error"))
    return out


def student(i, stop):
    pw = hashlib.sha256(str(i).encode()).hexdigest()
    r = post({"action": "signup", "name": f"Load Student {i}", "mobile": f"07{i:09d}", "email": f"load{i}@example.com",
              "university": "Load Uni", "course": "MSc", "pw": pw, "consent": True, "country": "IN"})
    tok = r.get("token")
    if not tok:
        return
    while time.time() < stop:
        k = random.random()
        if k < 0.55:
            post({"action": "events", "token": tok, "events": [{"t": "q", "topic": "eval", "id": random.randint(1, 140), "ok": random.random() < .7} for _ in range(5)]})
        elif k < 0.8:
            post({"action": "saveProgress", "token": tok, "progress": {"idta-hist": list(range(200)), "idta-lessons": ["l1", "l2"]}})
        elif k < 0.9:
            post({"action": "config"})
        elif k < 0.95:
            post({"action": "leaderboard"})
        else:
            post({"action": "me", "token": tok})
        time.sleep(random.uniform(0.2, 1.0))


stop = time.time() + SECS
th = [threading.Thread(target=student, args=(i, stop)) for i in range(N)]
t0 = time.time()
for t in th:
    t.start()
for t in th:
    t.join()
el = time.time() - t0
lat.sort()
print(f"{N} students for {el:.0f}s: {len(lat)} requests ({len(lat)/el:.0f}/s), errors {len(errs)} {set(errs) if errs else ''}")
print(f"latency p50 {statistics.median(lat)*1000:.0f} ms, p95 {lat[int(len(lat)*.95)]*1000:.0f} ms, p99 {lat[int(len(lat)*.99)]*1000:.0f} ms")
