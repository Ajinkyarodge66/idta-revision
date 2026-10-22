"""Database tables. Postgres in production, SQLite for local runs and tests."""
import re

from sqlalchemy import (BigInteger, Boolean, Column, Float, Index, Integer, MetaData, String, Table, Text,
                        create_engine, event)
from . import settings

md = MetaData()


def _seq():
    # insertion order (the old spreadsheets were ordered by row)
    return Column("seq", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)


users = Table("users", md, _seq(),
    Column("id", String(40), unique=True, nullable=False),
    Column("created", String(32), nullable=False),
    Column("name", String(60), nullable=False),
    Column("mobile", String(20), unique=True, nullable=False),
    Column("email", String(120), unique=True, nullable=False),
    Column("uni", String(100), default=""),
    Column("course", String(100), default=""),
    Column("hash", String(64), nullable=False),
    Column("salt", String(40), nullable=False),
    Column("lb", Boolean, default=True),
    Column("prog", Text, default=""),
    Column("prog_at", String(32), default=""),
    Column("last", String(32), default=""),
    Column("rcode", String(64), default=""),
    Column("rexp", String(32), default=""),
    Column("consent", String(32), default=""),
    Column("uni_email", String(120), default=""),
    Column("vuni", String(100), default=""),
    Column("vcode", String(64), default=""),
    Column("vexp", String(32), default=""),
    Column("exam", String(200), default=""),
    Column("exam_used", String(32), default=""),
    Column("ref_by", String(40), default=""),
    Column("ref_code", String(8), index=True),
    Column("src", String(40), default=""),
    Column("dev", String(10), default=""),
    Column("site", String(8), default="uk"),
    Column("country", String(2), default=""),
    Column("cday", Integer),                           # sign-up day (days since 1970), for retention
)
Index("ix_users_ref_by", users.c.ref_by)
Index("ix_users_uni_email", users.c.uni_email)

sessions = Table("sessions", md,
    Column("token_hash", String(64), primary_key=True),
    Column("uid", String(40), nullable=False, index=True),
    Column("created", String(32)),
    Column("expires", BigInteger, nullable=False),     # epoch ms
)

# Activity: answers (q), mocks, Calc Lab, plus server-written sales rows (unlock, revoke, refund, payout, restore)
events = Table("events", md, _seq(),
    Column("ts", BigInteger, nullable=False),          # epoch ms
    Column("uid", String(40), nullable=False),
    Column("name", String(60), default=""),
    Column("ev", String(10), nullable=False),
    Column("topic", String(20)),
    Column("qid", Float),
    Column("ok", Integer),
    Column("s", Float),                                # score, or amount for sales rows
    Column("n", Float),                                # out of
    Column("cur", String(3)),                          # currency for sales rows
    Column("k", String(80)),                           # detail
    Column("ins", BigInteger),                         # server insert time (epoch ms), used by the rollup
)
Index("ix_events_uid_seq", events.c.uid, events.c.seq)
Index("ix_events_ev_ts", events.c.ev, events.c.ts)
Index("ix_events_ts", events.c.ts)

news = Table("news", md, _seq(),
    Column("id", String(12), unique=True), Column("created", String(32)), Column("title", String(100)),
    Column("message", Text), Column("level", String(12)), Column("expires", String(32)), Column("audience", String(100), default=""))

questions = Table("questions", md, _seq(),
    Column("id", Integer, unique=True), Column("updated", String(32)), Column("t", String(10)), Column("type", String(10)),
    Column("p", Text), Column("o", Text), Column("a", Text), Column("e", Text), Column("audience", String(100), default=""))

coupons = Table("coupons", md, _seq(),
    Column("code", String(24), unique=True), Column("created", String(32)), Column("max", Integer), Column("used", Integer, default=0),
    Column("expires", String(32), default=""), Column("note", String(100), default=""), Column("used_by", Text, default=""),
    Column("value", Float, default=0))

certs = Table("certs", md, _seq(),
    Column("id", String(10), unique=True), Column("issued", String(32)), Column("uid", String(40), index=True),
    Column("name", String(60)), Column("score", Float), Column("n", Float), Column("exam", String(80)), Column("site", String(8)))

payments = Table("payments", md, _seq(),
    Column("id", String(10), unique=True), Column("submitted", String(32)), Column("uid", String(40), index=True),
    Column("name", String(60)), Column("mobile", String(20)), Column("amount", Float), Column("ref", String(80), default=""),
    Column("ref_norm", String(80), default="", index=True), Column("file", String(120)), Column("img_hash", String(64), index=True),
    Column("status", String(10)), Column("decided", String(32), default=""), Column("note", String(140), default=""),
    Column("cur", String(3)), Column("flags", Text, default=""), Column("ip", String(64), default=""))

refunds = Table("refunds", md, _seq(),
    Column("id", String(10), unique=True), Column("requested", String(32)), Column("uid", String(40), index=True),
    Column("name", String(60)), Column("mobile", String(20)), Column("amount", Float), Column("to", String(120)),
    Column("reason", String(220)), Column("status", String(10)), Column("decided", String(32), default=""),
    Column("note", String(140), default=""), Column("cur", String(3)))

payouts = Table("payouts", md, _seq(),
    Column("id", String(10), unique=True), Column("requested", String(32)), Column("uid", String(40), index=True),
    Column("name", String(60)), Column("mobile", String(20)), Column("amount", Float), Column("to", String(120)),
    Column("status", String(10)), Column("decided", String(32), default=""), Column("note", String(140), default=""))

visits = Table("visits", md, _seq(),
    Column("ts", BigInteger, index=True), Column("src", String(40)), Column("dev", String(10)),
    Column("li", Boolean), Column("ret", Boolean), Column("site", String(8)))

capstones = Table("capstones", md, _seq(),
    Column("id", String(10), unique=True), Column("submitted", String(32)), Column("uid", String(40), index=True),
    Column("name", String(60)), Column("title", String(100)), Column("repo", String(240)), Column("demo", String(340)),
    Column("summary", Text), Column("status", String(10)), Column("mark", Float), Column("feedback", Text, default=""),
    Column("marked", String(32), default=""), Column("cert_id", String(10), default=""))

proctor = Table("proctor", md, _seq(),
    Column("id", String(10), unique=True), Column("ts", BigInteger, index=True), Column("time", String(32)),
    Column("uid", String(40), index=True), Column("name", String(60)), Column("session", String(16)),
    Column("exam", String(80)), Column("reason", String(10)), Column("file", String(120)))

settings_t = Table("settings", md,
    Column("key", String(40), primary_key=True), Column("value", Text))

# ---- Running totals, kept up to date by app/rollup.py so dashboards never scan millions of answers ----
user_stats = Table("user_stats", md,
    Column("uid", String(40), primary_key=True), Column("last", BigInteger, default=0), Column("q", Integer, default=0),
    Column("qr", Integer, default=0), Column("mocks", Integer, default=0), Column("best", Float), Column("last_mock", String(20), default=""),
    Column("last_mock_seq", BigInteger, default=0), Column("lab", Integer, default=0), Column("labr", Integer, default=0))
Index("ix_user_stats_best", user_stats.c.best)
topic_stats = Table("topic_stats", md, Column("topic", String(20), primary_key=True), Column("correct", BigInteger, default=0), Column("total", BigInteger, default=0))
question_stats = Table("question_stats", md, Column("qid", String(20), primary_key=True), Column("correct", BigInteger, default=0), Column("total", BigInteger, default=0))
day_activity = Table("day_activity", md, Column("uid", String(40), primary_key=True), Column("day", Integer, primary_key=True))
Index("ix_day_activity_day", day_activity.c.day)

# Rate limits and flood guards, shared by every server instance
counters = Table("counters", md,
    Column("key", String(120), primary_key=True), Column("n", Integer, nullable=False), Column("exp", BigInteger, nullable=False))


def make_engine(url=None):
    url = url or settings.DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    if url.startswith("sqlite"):
        eng = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(eng, "connect")
        def _wal(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()
        return eng
    # prepare_threshold=None: Neon's connection pooler (PgBouncer, transaction mode) and server-side
    # prepared statements don't mix; every query is sent plainly instead.
    eng = create_engine(url, pool_size=settings.POOL_SIZE, max_overflow=settings.POOL_SIZE, pool_pre_ping=True, pool_recycle=300,
                        pool_timeout=15, connect_args={"prepare_threshold": None, "connect_timeout": 10})

    @event.listens_for(eng, "begin")
    def _timeout(conn):
        # no single request may hold the database for more than 25 s (works through the pooler)
        conn.exec_driver_sql("SET LOCAL statement_timeout = '25s'")
    return eng


engine = make_engine()


def _safe(e):
    """Error text without passwords (database errors can echo the connection string)."""
    return re.sub(r"(postgres(?:ql)?(?:\+\w+)?://[^:/@\s]+):[^@\s]+@", r"\1:***@", re.sub(r"npg_\w+", "npg_***", str(e)))[:600]


def init_db(eng=None):
    """Create missing tables. Several server processes start at once, so only one does it at a time."""
    eng = eng or engine
    try:
        _init_db(eng)
    except Exception as e:
        raise RuntimeError("Could not set up the database: " + _safe(e)) from None


UPGRADES = [   # columns and indexes added after the first release; each is safe to run again
    ("events", "ins", "BIGINT"),
    ("users", "cday", "INTEGER"),
]


def _upgrade(c, dialect):
    for table, col, typ in UPGRADES:
        if dialect == "postgresql":
            c.exec_driver_sql(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {typ}')
        else:
            cols = [r[1] for r in c.exec_driver_sql(f"PRAGMA table_info({table})")]
            if col not in cols:
                c.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    c.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_events_ts ON events (ts)")
    if dialect == "postgresql":
        c.exec_driver_sql("UPDATE users SET cday = floor(extract(epoch from created::timestamptz) / 86400)::int "
                          "WHERE cday IS NULL AND created <> ''")
    else:
        from .util import parse_ms
        for r in c.exec_driver_sql("SELECT id, created FROM users WHERE cday IS NULL").fetchall():
            ms = parse_ms(r[1])
            if ms is not None:
                c.exec_driver_sql("UPDATE users SET cday = ? WHERE id = ?", (ms // 86_400_000, r[0]))


def _init_db(eng):
    if eng.dialect.name == "postgresql":
        with eng.begin() as c:
            c.exec_driver_sql("SELECT pg_advisory_xact_lock(424242)")
            md.create_all(c)
            _upgrade(c, "postgresql")
        return
    for attempt in range(5):
        try:
            md.create_all(eng)
            with eng.begin() as c:
                _upgrade(c, "sqlite")
            return
        except Exception:
            if attempt == 4:
                raise
            import time
            time.sleep(0.3)
