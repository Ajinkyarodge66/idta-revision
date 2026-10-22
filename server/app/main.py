"""HTTP layer. Same contract as the old Apps Script web app:
POST / (or /exec) with a JSON body {action, ...}; GET ?action=ping|config|leaderboard|cert."""
import json
import logging
import time

import threading
from collections import defaultdict

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from sqlalchemy.exc import DBAPIError, OperationalError

from . import settings
from . import rollup
from .api import API, Ctx, fail, housekeeping
from .db import engine, init_db
from .services import bump, mailer, too_many

log = logging.getLogger("idta")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(title="IDTA Revision / JobReady AI backend", docs_url=None, redoc_url=None, openapi_url=None)
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402
app.add_middleware(GZipMiddleware, minimum_size=1500)   # leaderboard and admin pages shrink about 5x
HEADERS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
           "Access-Control-Allow-Headers": "Content-Type", "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
           "Referrer-Policy": "no-referrer"}
GET_ACTIONS = {"leaderboard", "config", "cert", "ping"}
DASHBOARDS = {"leaderboard", "admin", "adminAnalytics", "adminUsersPage"}   # bring running totals up to date first


class IpLimiter:
    """Per-server flood guard: each IP gets a generous budget per 10 seconds (a whole campus can share one IP)."""
    def __init__(self, per_10s):
        self.max, self.lock, self.win, self.n = per_10s, threading.Lock(), 0, defaultdict(int)

    def over(self, ip):
        w = int(time.time() // 10)
        with self.lock:
            if w != self.win:
                self.win, self.n = w, defaultdict(int)
            self.n[ip] += 1
            return self.n[ip] > self.max


limiter = IpLimiter(int(__import__("os").environ.get("IP_LIMIT_PER_10S", "1500")))
_next_housekeeping = 0.0


@app.on_event("startup")
def _startup():
    init_db()
    if not settings.TEST_HARNESS:
        threading.Thread(target=_rollup_loop, daemon=True).start()


def _rollup_loop():
    """Keep the running totals fresh in the background (every ~10 s while the server is busy)."""
    while True:
        try:
            rollup.run(max_batches=5)
        except Exception as e:
            log.warning("rollup failed: %s", e)
        time.sleep(10)


def _ip(req: Request):
    xf = req.headers.get("x-forwarded-for", "")
    return (xf.split(",")[0].strip() if xf else (req.client.host if req.client else ""))[:64]


def _housekeeping():
    """Hourly clean-up (expired sessions, old exam photos, stale counters), shared across instances."""
    global _next_housekeeping
    if time.time() < _next_housekeeping:
        return
    _next_housekeeping = time.time() + 3600
    try:
        with engine.begin() as c:
            if too_many(c, "housekeeping", 1):
                return
            bump(c, "housekeeping", 3600)
            housekeeping(c)
    except Exception as e:
        log.warning("housekeeping failed: %s", e)


def run(name, b, ip):
    fn = API.get(name)
    if not fn:
        return fail("unknown_action")
    if ip and limiter.over(ip):
        return fail("busy")
    if name in DASHBOARDS:
        try:
            rollup.run(max_batches=3, force=True)
        except Exception as e:
            log.warning("rollup failed: %s", e)
    for attempt in range(3):
        try:
            with engine.begin() as conn:
                ctx = Ctx(conn, ip)
                res = fn(ctx, b)
            break
        except OperationalError as e:      # deadlock / serialization / lost connection: retry
            if attempt == 2:
                log.exception("db error in %s", name)
                return fail("busy")
            time.sleep(0.05 * (attempt + 1))
        except DBAPIError:
            log.exception("db error in %s", name)
            return fail("server_error")
        except Exception:
            log.exception("error in %s", name)
            return fail("server_error")
    for m in ctx.mails:                    # only after the data is safely saved
        mailer.send(*m)
    _housekeeping()
    return res


def reply(obj, status=200):
    return JSONResponse(obj, status_code=status, headers=HEADERS)


async def _post(req: Request):
    size = int(req.headers.get("content-length") or 0)
    if size > settings.MAX_BODY:
        return reply(fail("too_big"), 413)
    raw = b""
    async for chunk in req.stream():
        raw += chunk
        if len(raw) > settings.MAX_BODY:
            return reply(fail("too_big"), 413)
    try:
        b = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return reply(fail("bad_request"))
    if not isinstance(b, dict):
        return reply(fail("bad_request"))
    return reply(await run_in_threadpool(run, b.get("action"), b, _ip(req)))


async def _get(req: Request):
    p = dict(req.query_params)
    name = p.get("action") if p.get("action") in GET_ACTIONS else "ping"
    return reply(await run_in_threadpool(run, name, p, _ip(req)))


async def _options(req: Request):
    return Response(status_code=204, headers=HEADERS)


app.add_api_route("/exec", _post, methods=["POST"])
app.add_api_route("/exec", _get, methods=["GET"])
app.add_api_route("/exec", _options, methods=["OPTIONS"])
app.add_api_route("/healthz", lambda: {"ok": True}, methods=["GET"])
app.add_api_route("/health", lambda: {"ok": True}, methods=["GET"])     # /healthz is reserved on Cloud Run

if settings.TEST_HARNESS:
    from . import harness
    harness.mount(app)
else:
    app.add_api_route("/", _post, methods=["POST"])
    app.add_api_route("/", _get, methods=["GET"])
    app.add_api_route("/", _options, methods=["OPTIONS"])
