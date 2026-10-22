"""End-to-end test harness only (TEST_HARNESS=1): serves the built pages and a /debug view of what was saved,
mirroring the Node stub the browser tests were written against. Never enabled in production."""
import os

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import func, select

from .db import engine, events, proctor, sessions, users
from .services import mailer

STATE = {"down": False}
COLS = ["id", "created", "name", "mobile", "email", "uni", "course", "hash", "salt", "lb", "prog", "prog_at", "last", "rcode", "rexp", "consent",
        "uni_email", "vuni", "vcode", "vexp", "exam", "exam_used", "ref_by", "src", "dev", "site", "country"]
HEAD = '<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">'


def _fix(h):
    port = os.environ.get("PORT", "8766")
    return (h.replace('const SYNC_URL = "";', f'const SYNC_URL = "http://localhost:{port}/exec";')
             .replace("https://ajinkyarodge66.github.io/idta-revision/", f"http://localhost:{port}/")
             .replace("https://ajinkyarodge66.github.io/jobready-ai/", f"http://localhost:{port}/g/"))


def _read(p):
    try:
        with open(p, encoding="utf-8") as f:
            return _fix(f.read())
    except OSError:
        return ""


def mount(app):
    page = _read(os.environ.get("PAGE", "/home/claude/idta/public-artifact.html"))
    gpage = _read(os.environ.get("GPAGE", "/home/claude/idta/global-artifact.html"))

    @app.on_event("startup")
    def seed():
        from .api import set_setting
        with engine.begin() as c:
            if not os.environ.get("EXAMLOCK"):
                set_setting(c, "EXAM_CFG", '{"lock":false}')

    @app.middleware("http")
    async def down(req: Request, call_next):
        if STATE["down"] and req.url.path == "/exec":
            return Response("down", status_code=503)
        return await call_next(req)

    @app.get("/")
    def root():
        return HTMLResponse(HEAD + page)

    @app.get("/g/")
    def groot():
        return HTMLResponse(HEAD + gpage)

    @app.get("/assets/{name}")
    def assets(name: str):
        try:
            with open("/home/claude/idta-revision/assets/" + os.path.basename(name), "rb") as f:
                return Response(f.read(), media_type="image/png")
        except OSError:
            return Response(status_code=404)

    @app.get("/down")
    def set_down(v: str = "0"):
        STATE["down"] = v == "1"
        return Response("ok")

    @app.get("/debug")
    def debug():
        with engine.connect() as c:
            rows = [["ID", "Created", "Name"] + [""] * (len(COLS) - 3)]
            for r in c.execute(select(users).order_by(users.c.seq)).mappings():
                row = [r[k] for k in COLS]
                row[9] = "yes" if r["lb"] else "no"
                rows.append(["" if v is None else v for v in row])
            return JSONResponse({"mails": mailer.sent, "photos": c.execute(select(func.count()).select_from(proctor)).scalar(), "users": rows,
                                 "sessions": c.execute(select(func.count()).select_from(sessions)).scalar() + 1,   # +1: the old sheets counted their header row
                                 "events": c.execute(select(func.count()).select_from(events)).scalar() + 1})
