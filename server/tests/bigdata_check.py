"""Scale check: fill a Postgres database with lots of students and answers, then time the heavy endpoints.

    BIG_DB=postgresql://... USERS=300000 EVENTS=6000000 python tests/bigdata_check.py
"""
import json
import os
import sys
import time

os.environ["DATABASE_URL"] = os.environ["BIG_DB"]
os.environ.setdefault("ADMIN_KEY", "k")
os.environ.setdefault("MAIL_MODE", "log")
os.environ.setdefault("ROLLUP_LAG_MS", "0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import db, main, rollup  # noqa: E402

N_USERS, N_EVENTS = int(os.environ.get("USERS", 300000)), int(os.environ.get("EVENTS", 6000000))
eng = db.engine


def t(label, fn):
    t0 = time.time()
    out = fn()
    print(f"{label:<44} {time.time() - t0:7.2f} s")
    return out


def seed():
    db.md.drop_all(eng)
    db.init_db()
    now = int(time.time() * 1000)
    from sqlalchemy import create_engine
    raw = create_engine(os.environ["BIG_DB"].replace("postgresql://", "postgresql+psycopg://"))   # no 25 s limit for bulk seeding
    with raw.begin() as c:
        X = lambda sql: c.exec_driver_sql(sql.replace("%", "%%"))   # psycopg treats % as a placeholder
        X(f"""
          INSERT INTO users (id, created, name, mobile, email, uni, course, hash, salt, lb, prog, ref_code, src, dev, site, country, cday)
          SELECT 'u' || i, to_char(to_timestamp(({now} - (i % 90) * 86400000 - (i * 37 % 86400000)) / 1000.0) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
                 'Student ' || i, (7000000000 + i)::text, 's' || i || '@example.com',
                 CASE WHEN i % 7 = 0 THEN 'University of Portsmouth' ELSE 'Uni ' || (i % 50) END, 'MSc ' || (i % 12), repeat('a', 64), 'salt', (i % 4 <> 0),
                 '{{"idta-lessons":["l1","l2"]}}', upper(substr(md5(i::text), 1, 8)),
                 (ARRAY['direct','whatsapp','google','referral','linkedin'])[1 + i % 5], (ARRAY['phone','desktop','tablet'])[1 + i % 3],
                 CASE WHEN i % 3 = 0 THEN 'global' ELSE 'uk' END, (ARRAY['GB','IN','NG','PK','US'])[1 + i % 5],
                 (({now} - (i % 90) * 86400000) / 86400000)::int
          FROM generate_series(1::bigint, {N_USERS}) i""")
        X(f"""
          INSERT INTO events (ts, uid, name, ev, topic, qid, ok, s, n, k)
          SELECT {now} - (random() * 90 * 86400000)::bigint, 'u' || (1 + (random() * ({N_USERS} - 1))::int), 'Student',
                 CASE WHEN r < 0.85 THEN 'q' WHEN r < 0.9 THEN 'mock' ELSE 'lab' END,
                 (ARRAY['data','dist','prep','cls','eval','nn','ens','reg','arm','clu','py','math','nlp','genai'])[1 + (random() * 13)::int],
                 (random() * 140)::int, (random() < 0.7)::int,
                 CASE WHEN r >= 0.85 AND r < 0.9 THEN (random() * 30)::int END, CASE WHEN r >= 0.85 AND r < 0.9 THEN 30 END,
                 CASE WHEN r >= 0.85 AND r < 0.9 THEN md5(random()::text) END
          FROM (SELECT random() AS r FROM generate_series(1, {N_EVENTS})) x""")
        X(f"""
          INSERT INTO events (ts, uid, name, ev, s, cur, k)
          SELECT {now} - (random() * 60 * 86400000)::bigint, 'u' || i, 'Student', 'unlock', 199, 'INR', 'payment'
          FROM generate_series(1::bigint, {N_USERS}, 20) i""")
        X(f"""
          INSERT INTO visits (ts, src, dev, li, ret, site)
          SELECT {now} - (random() * 90 * 86400000)::bigint, (ARRAY['direct','whatsapp','google'])[1 + (random() * 2)::int], 'phone', random() < 0.3, random() < 0.5, 'uk'
          FROM generate_series(1, {N_USERS * 3})""")
    with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as c2:
        c2.exec_driver_sql("ANALYZE")


if os.environ.get("SKIP_SEED") != "1":
    t(f"seed {N_USERS:,} students, {N_EVENTS:,} answers", seed)
    t("build running totals from scratch (one-off)", rollup.rebuild)
cl = TestClient(main.app)


def call(**b):
    r = cl.post("/", content=json.dumps(b))
    d = r.json()
    assert d.get("ok"), d
    return d


lb = t("leaderboard (first, uncached)", lambda: call(action="leaderboard"))
t("leaderboard for one university", lambda: call(action="leaderboard", uni="University of Portsmouth"))
ad = t("admin overview", lambda: call(action="admin", key="k"))
t("admin search 'student 12345'", lambda: call(action="admin", key="k", q="student 12345"))
t("analytics: last 30 days", lambda: call(action="adminAnalytics", key="k", days=30))
an = t("analytics: last 365 days, JobReady only", lambda: call(action="adminAnalytics", key="k", days=365, site="global"))
t("export page of 2,000 students", lambda: call(action="adminUsersPage", key="k", offset=100000, limit=2000))
t("sales list", lambda: call(action="adminSales", key="k"))
print(f"\nleaderboard rows {len(lb['leaderboard'])}, players {lb['players']:,}; admin shows {len(ad['users'])} of {ad['totals']['accounts']:,}, "
      f"answers {ad['totals']['q']:,}; analytics funnel {an['funnel']}")
