"""Bento admin panel (CRM): login, buyurtmalar, statistika."""
import hashlib
import hmac
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import web

import db

TZ = ZoneInfo("Asia/Tashkent")
ROOT = Path(__file__).parent
COOKIE = "bento_panel"
SESSION_DAYS = 30

_cfg: dict = {}


def _sign(msg: str) -> str:
    return hmac.new(_cfg["secret"], msg.encode(), hashlib.sha256).hexdigest()[:32]


def _verify(token: str, kind: str) -> int | None:
    try:
        uid, exp, sig = token.split(".")
        uid_i, exp_i = int(uid), int(exp)
    except (ValueError, AttributeError):
        return None
    if exp_i < time.time() or not hmac.compare_digest(sig, _sign(f"{kind}:{uid_i}:{exp_i}")):
        return None
    return uid_i if uid_i in _cfg["ids"] else None


def current_user(req) -> int | None:
    return _verify(req.cookies.get(COOKIE, ""), "session")


def need_login(handler):
    async def wrapped(req):
        if current_user(req) is None:
            return web.json_response({"ok": False, "error": "login"}, status=401)
        return await handler(req)
    return wrapped


LOGIN_PAGE = """<!doctype html><html lang="uz"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Bento panel</title>
<style>body{font-family:system-ui,sans-serif;background:#f5f3ef;display:grid;place-items:center;min-height:100vh;margin:0}
.c{background:#fff;padding:32px;border-radius:16px;max-width:360px;text-align:center;box-shadow:0 4px 24px #0001}
h1{margin:0 0 8px;font-size:22px}p{color:#555;line-height:1.5}code{background:#f0ece4;padding:2px 6px;border-radius:6px}</style>
</head><body><div class="c"><h1>🍱 Bento panel</h1>
<p>Kirish uchun Telegram'da botga <code>/panel</code> deb yozing — bot sizga shaxsiy kirish havolasini yuboradi.</p>
<p style="font-size:13px;color:#999">MSG</p></div></body></html>"""


def login_page(msg: str = "", status: int = 200):
    return web.Response(text=LOGIN_PAGE.replace("MSG", msg), content_type="text/html", status=status)


async def login(req):
    uid = _verify(req.query.get("t", ""), "login")
    if uid is None:
        return login_page("Havola eskirgan yoki noto'g'ri. Botga qaytadan /panel yozing.", 403)
    exp = int(time.time()) + SESSION_DAYS * 86400
    resp = web.HTTPFound("/admin")
    resp.set_cookie(COOKIE, f"{uid}.{exp}.{_sign(f'session:{uid}:{exp}')}", max_age=SESSION_DAYS * 86400,
                    httponly=True, secure=req.secure or req.headers.get("X-Forwarded-Proto") == "https",
                    samesite="Lax", path="/")
    return resp


async def logout(_req):
    resp = web.HTTPFound("/admin")
    resp.del_cookie(COOKIE, path="/")
    return resp


async def page(req):
    if current_user(req) is None:
        return login_page()
    f = ROOT / "admin.html"
    return web.FileResponse(f, headers={"Cache-Control": "no-cache"})


@need_login
async def api_summary(_req):
    return web.json_response(db.summary(datetime.now(TZ).date()))


def _day(s: str | None, default: date) -> str:
    try:
        return date.fromisoformat(s).isoformat() if s else default.isoformat()
    except ValueError:
        return default.isoformat()


@need_login
async def api_orders(req):
    today = datetime.now(TZ).date()
    d_from = _day(req.query.get("from"), today)
    d_to = _day(req.query.get("to"), today)
    return web.json_response({"orders": db.list_orders(d_from, d_to), "from": d_from, "to": d_to})


@need_login
async def api_set_status(req):
    try:
        oid = int(req.match_info["id"])
        status = (await req.json()).get("status")
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "bad request"}, status=400)
    if status not in db.STATUSES:
        return web.json_response({"ok": False, "error": "status"}, status=400)
    names = _cfg.get("names", {})
    uid = current_user(req)
    o = await _cfg["apply"](oid, status, f"Panel ({names.get(uid, uid)})")
    if not o:
        return web.json_response({"ok": False, "error": "topilmadi"}, status=404)
    return web.json_response({"ok": True, "order": o})


@need_login
async def api_me(req):
    return web.json_response({"id": current_user(req)})


def setup(app: web.Application, ids: set[int], secret: bytes, apply_status):
    _cfg.update(ids=ids, secret=secret, apply=apply_status)
    app.router.add_get("/admin", page)
    app.router.add_get("/admin/", page)
    app.router.add_get("/admin/login", login)
    app.router.add_get("/admin/logout", logout)
    app.router.add_get("/admin/api/me", api_me)
    app.router.add_get("/admin/api/summary", api_summary)
    app.router.add_get("/admin/api/orders", api_orders)
    app.router.add_post("/admin/api/orders/{id}/status", api_set_status)

