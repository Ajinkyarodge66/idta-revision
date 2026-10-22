"""Running totals for dashboards.

Every answer is still stored in `events`, but the leaderboard, admin overview and analytics read small summary
tables instead of scanning every answer ever given. This module folds new events into those tables in batches.
It runs at most once every few seconds per server, never twice at the same time, and only takes rows that are at
least ROLLUP_LAG_MS old, so a transaction that is still committing can never be skipped."""
import os
import threading
import time

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.dialects import postgresql, sqlite

from .db import day_activity, engine, events, question_stats, settings_t, topic_stats, user_stats
from .util import js_str, now_ms, out_num

LAG_MS = int(os.environ.get("ROLLUP_LAG_MS", "90000"))
BATCH = 20000
DAY = 86_400_000
_local = threading.Lock()
_next = [0.0]


def _ins(c, table):
    return (postgresql if c.dialect.name == "postgresql" else sqlite).insert(table)


def _cursor(c):
    r = c.execute(select(settings_t.c.value).where(settings_t.c.key == "ROLLUP_SEQ")).first()
    return int(r.value) if r else 0


def _set_cursor(c, seq):
    st = _ins(c, settings_t).values(key="ROLLUP_SEQ", value=str(seq))
    c.execute(st.on_conflict_do_update(index_elements=["key"], set_={"value": st.excluded.value}))


def _fold(c, rows):
    us, tp, qs, days = {}, {}, {}, set()
    for r in rows:
        if not r.uid:
            continue
        u = us.setdefault(r.uid, {"uid": r.uid, "last": 0, "q": 0, "qr": 0, "mocks": 0, "best": None, "last_mock": "", "last_mock_seq": 0, "lab": 0, "labr": 0})
        u["last"] = max(u["last"], r.ts or 0)
        if r.ev == "q":
            u["q"] += 1
            u["qr"] += 1 if r.ok == 1 else 0
            if r.topic:
                t = tp.setdefault(r.topic, [0, 0]); t[1] += 1; t[0] += 1 if r.ok == 1 else 0
            if r.qid is not None:
                k = js_str(out_num(r.qid)); q = qs.setdefault(k, [0, 0]); q[1] += 1; q[0] += 1 if r.ok == 1 else 0
        elif r.ev == "mock":
            u["mocks"] += 1
            u["last_mock"], u["last_mock_seq"] = js_str(out_num(r.s)) + "/" + js_str(out_num(r.n)), r.seq
            if r.n == 30 and r.s is not None and (u["best"] is None or r.s > u["best"]):
                u["best"] = r.s
        elif r.ev == "lab":
            u["lab"] += 1
            u["labr"] += 1 if r.ok == 1 else 0
        if r.ev in ("q", "mock", "lab") and r.ts:
            days.add((r.uid, r.ts // DAY))
    if us:
        st = _ins(c, user_stats)
        ex, T = st.excluded, user_stats.c
        c.execute(st.on_conflict_do_update(index_elements=["uid"], set_={
            "last": case((ex["last"] > T["last"], ex["last"]), else_=T["last"]),
            "q": T.q + ex.q, "qr": T.qr + ex.qr, "mocks": T.mocks + ex.mocks, "lab": T.lab + ex.lab, "labr": T.labr + ex.labr,
            "best": case((and_(ex.best.is_not(None), or_(T.best.is_(None), ex.best > T.best)), ex.best), else_=T.best),
            "last_mock": case((ex.last_mock_seq > func.coalesce(T.last_mock_seq, 0), ex.last_mock), else_=T.last_mock),
            "last_mock_seq": case((ex.last_mock_seq > func.coalesce(T.last_mock_seq, 0), ex.last_mock_seq), else_=T.last_mock_seq),
        }), list(us.values()))
    for table, key, data in ((topic_stats, "topic", tp), (question_stats, "qid", qs)):
        if data:
            st = _ins(c, table)
            c.execute(st.on_conflict_do_update(index_elements=[key], set_={
                "correct": table.c.correct + st.excluded.correct, "total": table.c.total + st.excluded.total}),
                [{key: k, "correct": v[0], "total": v[1]} for k, v in data.items()])
    if days:
        c.execute(_ins(c, day_activity).on_conflict_do_nothing(), [{"uid": u, "day": d} for u, d in days])


def run(max_batches=10, force=False):
    """Fold new events into the summary tables. Returns how many events were processed."""
    if not force and time.time() < _next[0]:
        return 0
    if not _local.acquire(blocking=False):
        return 0
    try:
        _next[0] = time.time() + 5
        done = 0
        for _ in range(max_batches):
            with engine.begin() as c:
                if c.dialect.name == "postgresql" and not c.exec_driver_sql("SELECT pg_try_advisory_xact_lock(424243)").scalar():
                    return done          # another server is doing it right now
                cur = _cursor(c)
                cutoff = now_ms() - LAG_MS
                rows = c.execute(select(events.c.seq, events.c.ts, events.c.uid, events.c.ev, events.c.topic, events.c.qid, events.c.ok,
                                        events.c.s, events.c.n).where(events.c.seq > cur).order_by(events.c.seq).limit(BATCH)
                                 .where(or_(events.c.ins.is_(None), events.c.ins <= cutoff))).all()
                # stop at the first row that is too new, so nothing behind it is ever skipped
                newest = c.execute(select(func.min(events.c.seq)).where(and_(events.c.seq > cur, events.c.ins > cutoff))).scalar()
                if newest is not None:
                    rows = [r for r in rows if r.seq < newest]
                if not rows:
                    return done
                _fold(c, rows)
                _set_cursor(c, rows[-1].seq)
                done += len(rows)
                if len(rows) < BATCH:
                    return done
        return done
    finally:
        _local.release()


def rebuild():
    """Recompute every summary from scratch (used once when upgrading, or to repair)."""
    with engine.begin() as c:
        for t in (user_stats, topic_stats, question_stats, day_activity):
            c.execute(t.delete())
        _set_cursor(c, 0)
    while run(max_batches=50, force=True):
        pass
