"""
Bento — Telegram Mini App bot (tushlik buyurtmasi).

- Mijoz botda "🍱 Меню" tugmasini bosadi -> mini app ochiladi -> savat -> buyurtma.
- Buyurtma Telegram guruhga tushadi, operator "Қабул қилинди" / "Йўлга чиқди" tugmalarini bosadi,
  mijozga avtomatik xabar boradi.
- Menyuni adminlar bot bilan shaxsiy chatda yangilaydi (rasm + izoh).
- Menyu guruhdagi pin qilingan xabarda saqlanadi, shuning uchun server qayta ishga tushsa ham yo'qolmaydi.
"""
import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import time
from datetime import datetime, time as dtime
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bento")

# ---------------- Sozlamalar (.env / hosting "Environment" bo'limidan) ----------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
GROUP_ID = int(os.environ["GROUP_ID"])                      # buyurtmalar tushadigan guruh
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")       # https://bento-xxx.onrender.com
DELIVERY_FEE = int(os.environ.get("DELIVERY_FEE", "15000"))  # 3 tadan kam bo'lsa yetkazish narxi
FREE_FROM = int(os.environ.get("FREE_DELIVERY_FROM", "3"))   # shuncha porsiyadan boshlab bepul
ORDER_START = os.environ.get("ORDER_START", "09:00")
ORDER_END = os.environ.get("ORDER_END", "12:00")
MAX_SLOTS = int(os.environ.get("MAX_DISHES", "12"))
USE_POLLING = os.environ.get("USE_POLLING", "0") == "1"     # lokal test uchun
PORT = int(os.environ.get("PORT", "8080"))
TZ = ZoneInfo("Asia/Tashkent")

ROOT = Path(__file__).parent
WEBAPP_DIR = ROOT / "webapp" if (ROOT / "webapp" / "index.html").exists() else ROOT
MENU_FILE = ROOT / "menu.json"
MENU_MARK = "#BENTO_MENU"
WEBHOOK_PATH = "/tg/" + hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:24]
WEBHOOK_SECRET = hashlib.sha256(("secret:" + BOT_TOKEN).encode()).hexdigest()[:32]

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
router = Router()
dp.include_router(router)

menu: dict[str, dict] = {}          # {"1": {"name":..., "price":..., "desc":..., "photo": file_id}}
menu_msg_id: int | None = None
photo_cache: dict[str, bytes] = {}
order_counter = {"date": None, "n": 0}


# ---------------- Yordamchi funksiyalar ----------------
def hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def is_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(TZ)
    return hhmm(ORDER_START) <= now.time() < hhmm(ORDER_END)


def som(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " сўм"


def delivery_fee(total_qty: int) -> int:
    return 0 if total_qty >= FREE_FROM else DELIVERY_FEE


def sorted_menu():
    return sorted(menu.items(), key=lambda kv: int(kv[0]))


def check_init_data(init_data: str) -> dict | None:
    """Telegram WebApp initData imzosini tekshiradi (soxta buyurtmalardan himoya)."""
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received = pairs.pop("hash", None)
    if not received:
        return None
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        return None
    if time.time() - int(pairs.get("auth_date", "0")) > 24 * 3600:
        return None
    try:
        return json.loads(pairs.get("user", "{}")) or None
    except json.JSONDecodeError:
        return None


def next_order_no() -> int:
    today = datetime.now(TZ).date()
    if order_counter["date"] != today:
        order_counter.update(date=today, n=0)
    order_counter["n"] += 1
    return order_counter["n"]


def parse_dish(text: str):
    """'1 | Ош | 35000 | тавсиф' -> ("1", {...})"""
    parts = [p.strip() for p in text.split("|")]
    if len(parts) < 3 or not parts[0].isdigit() or not (1 <= int(parts[0]) <= MAX_SLOTS):
        return None
    price = int("".join(ch for ch in parts[2] if ch.isdigit()) or 0)
    if not parts[1] or price <= 0:
        return None
    desc = parts[3] if len(parts) > 3 else ""
    return str(int(parts[0])), {"name": parts[1][:60], "price": price, "desc": desc[:200]}


# ---------------- Menyuni saqlash (guruhdagi pin xabar) ----------------
def menu_text() -> str:
    lines = [MENU_MARK, "📋 Бугунги меню (тизим учун, ўчирманг ва пиндан олманг)", ""]
    for slot, d in sorted_menu():
        lines.append(f"{slot}. {d['name']} — {som(d['price'])}")
    if not menu:
        lines.append("(бўш)")
    lines += ["", json.dumps(menu, ensure_ascii=False, separators=(",", ":"))]
    return "\n".join(lines)


async def load_menu():
    global menu, menu_msg_id
    try:
        chat = await bot.get_chat(GROUP_ID)
        pm = chat.pinned_message
        if pm and pm.text and pm.text.startswith(MENU_MARK) and pm.from_user and pm.from_user.id == bot.id:
            menu = json.loads(pm.text.rsplit("\n", 1)[-1])
            menu_msg_id = pm.message_id
            log.info("Menyu guruhdan yuklandi: %d ta taom", len(menu))
            return
    except Exception as e:  # noqa: BLE001
        log.warning("Guruhdan menyuni o'qib bo'lmadi: %s", e)
    for f in (MENU_FILE, ROOT / "menu_default.json"):
        if f.exists():
            menu = json.loads(f.read_text("utf-8"))
            log.info("Menyu %s fayldan yuklandi: %d ta taom", f.name, len(menu))
            return


async def save_menu():
    global menu_msg_id
    MENU_FILE.write_text(json.dumps(menu, ensure_ascii=False), "utf-8")
    text = menu_text()
    if menu_msg_id:
        try:
            await bot.edit_message_text(text, chat_id=GROUP_ID, message_id=menu_msg_id, parse_mode=None)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
            log.warning("Menyu xabarini tahrirlab bo'lmadi, yangisi yuboriladi: %s", e)
    m = await bot.send_message(GROUP_ID, text, parse_mode=None, disable_notification=True)
    menu_msg_id = m.message_id
    try:
        await bot.pin_chat_message(GROUP_ID, m.message_id, disable_notification=True)
    except TelegramBadRequest as e:
        log.warning("Pin qilib bo'lmadi (botga 'Pin messages' huquqini bering): %s", e)


# ---------------- Bot: mijozlar ----------------
def open_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🍱 Менюни очиш", web_app=WebAppInfo(url=BASE_URL + "/"))
    ]])


@router.message(CommandStart(), F.chat.type == "private")
async def on_start(m: Message):
    await m.answer(
        "Ассалому алайкум! 👋\n<b>Bento</b> — тушлик · тез · иссиқ 🍱\n\n"
        f"Ҳар куни янги меню. Буюртмалар <b>{ORDER_START}–{ORDER_END}</b> оралиғида қабул қилинади.\n"
        f"{FREE_FROM} та ва ундан кўп порцияга етказиш <b>бепул</b>.\n\n"
        "Пастдаги тугмани босинг 👇",
        reply_markup=open_kb(),
    )


@router.message(Command("id"))
async def on_id(m: Message):
    await m.answer(f"Chat ID: <code>{m.chat.id}</code>\nSizning ID: <code>{m.from_user.id}</code>")


# ---------------- Bot: adminlar (menyu) ----------------
ADMIN_HELP = (
    "<b>Менюни янгилаш</b>\n\n"
    "Расм юборинг, изоҳига (caption) ёзинг:\n"
    "<code>1 | Ош | 35000 | Мол гўшти, сабзи, салат билан</code>\n"
    "(рақам | номи | нархи | тавсиф — тавсиф ихтиёрий)\n\n"
    "Расмсиз ҳам бўлади — шунчаки матн юборинг.\n"
    "Шу рақамдаги таом аввал бор бўлса — алмаштирилади.\n\n"
    "/menyu — ҳозирги меню\n"
    "/ochir 3 — 3-таомни ўчириш\n"
    "/tozala — бутун менюни тозалаш"
)


def admin_only(m: Message) -> bool:
    return m.chat.type == "private" and m.from_user is not None and m.from_user.id in ADMIN_IDS


@router.message(Command("yordam"), admin_only)
async def on_help(m: Message):
    await m.answer(ADMIN_HELP)


@router.message(Command("menyu"), admin_only)
async def on_menu(m: Message):
    if not menu:
        await m.answer("Меню бўш.\n\n" + ADMIN_HELP)
        return
    lines = [f"{s}. <b>{html.escape(d['name'])}</b> — {som(d['price'])}{' 📷' if d.get('photo') or d.get('photo_url') else ''}"
             for s, d in sorted_menu()]
    await m.answer("📋 <b>Ҳозирги меню</b>\n\n" + "\n".join(lines))


@router.message(Command("ochir"), admin_only)
async def on_remove(m: Message, command: CommandObject):
    slot = (command.args or "").strip()
    if slot in menu:
        removed = menu.pop(slot)
        await save_menu()
        await m.answer(f"🗑 {html.escape(removed['name'])} ўчирилди.")
    else:
        await m.answer("Масалан: <code>/ochir 3</code>")


@router.message(Command("tozala"), admin_only)
async def on_clear(m: Message):
    menu.clear()
    await save_menu()
    await m.answer("Меню тозаланди. Янги таомларни юборинг.")


@router.message(F.photo, admin_only)
async def on_dish_photo(m: Message):
    parsed = parse_dish(m.caption or "")
    if not parsed:
        await m.answer("Изоҳ нотўғри.\n\n" + ADMIN_HELP)
        return
    slot, dish = parsed
    dish["photo"] = m.photo[-1].file_id
    menu[slot] = dish
    await save_menu()
    await m.answer(f"✅ {slot}. {html.escape(dish['name'])} — {som(dish['price'])} сақланди.")


@router.message(F.text.contains("|"), admin_only)
async def on_dish_text(m: Message):
    parsed = parse_dish(m.text)
    if not parsed:
        await m.answer("Формат нотўғри.\n\n" + ADMIN_HELP)
        return
    slot, dish = parsed
    for key in ("photo", "photo_url"):        # eski rasm qoladi
        if slot in menu and menu[slot].get(key):
            dish[key] = menu[slot][key]
    menu[slot] = dish
    await save_menu()
    await m.answer(f"✅ {slot}. {html.escape(dish['name'])} — {som(dish['price'])} сақланди.")


@router.message(F.chat.type == "private")
async def on_other(m: Message):
    if m.from_user and m.from_user.id in ADMIN_IDS:
        await m.answer(ADMIN_HELP)
    else:
        await m.answer("Буюртма бериш учун менюни очинг 👇", reply_markup=open_kb())


# ---------------- Bot: operator tugmalari ----------------
STATUS = {
    "ok": ("✅ Қабул қилинди", "✅ Буюртмангиз қабул қилинди! Тайёрланмоқда 🍱"),
    "go": ("🚚 Йўлга чиқди", "🚚 Буюртмангиз йўлга чиқди! Тез орада етиб боради."),
}


def order_kb(uid: int | str, no: int | str, step: str = "ok") -> InlineKeyboardMarkup:
    label = STATUS[step][0]
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=f"st:{step}:{uid}:{no}")
    ]])


@router.callback_query(F.data.startswith("st:"))
async def on_status(cq: CallbackQuery):
    _, step, uid, no = cq.data.split(":")
    label, customer_text = STATUS[step]
    try:
        await bot.send_message(int(uid), f"{customer_text}\nБуюртма №{no}")
    except Exception as e:  # noqa: BLE001
        log.warning("Mijozga yozib bo'lmadi: %s", e)
    who = html.escape(cq.from_user.full_name)
    new_text = f"{cq.message.html_text}\n\n{label} — {who}, {datetime.now(TZ):%H:%M}"
    kb = order_kb(uid, no, "go") if step == "ok" else None
    try:
        await cq.message.edit_text(new_text, reply_markup=kb)
    except TelegramBadRequest as e:
        log.warning("Xabarni tahrirlab bo'lmadi: %s", e)
    await cq.answer(label)


# ---------------- Web: mini app va API ----------------
def json_err(msg: str, status: int = 400):
    return web.json_response({"ok": False, "error": msg}, status=status)


async def index(_req):
    return web.FileResponse(WEBAPP_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def api_menu(_req):
    dishes = []
    for slot, d in sorted_menu():
        photo = None
        if d.get("photo"):
            photo = f"/photo/{slot}?v={hashlib.md5(d['photo'].encode()).hexdigest()[:8]}"
        elif d.get("photo_url"):
            photo = d["photo_url"]
        dishes.append({"slot": slot, "name": d["name"], "price": d["price"], "desc": d.get("desc", ""), "photo": photo})
    return web.json_response(
        {"dishes": dishes, "open": is_open(), "start": ORDER_START, "end": ORDER_END,
         "fee": DELIVERY_FEE, "freeFrom": FREE_FROM},
        headers={"Cache-Control": "no-store"},
    )


async def photo(req):
    d = menu.get(req.match_info["slot"])
    if not d or not d.get("photo"):
        raise web.HTTPNotFound()
    fid = d["photo"]
    if fid not in photo_cache:
        f = await bot.get_file(fid)
        buf = BytesIO()
        await bot.download_file(f.file_path, buf)
        if len(photo_cache) > 40:
            photo_cache.pop(next(iter(photo_cache)))
        photo_cache[fid] = buf.getvalue()
    return web.Response(body=photo_cache[fid], content_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


async def api_order(req):
    try:
        body = await req.json()
    except Exception:  # noqa: BLE001
        return json_err("Нотўғри сўров")
    user = check_init_data(str(body.get("initData", "")))
    if not user:
        return json_err("Илтимос, менюни Telegram бот орқали очинг.", 401)
    if not is_open():
        return json_err(f"Буюртмалар фақат {ORDER_START}–{ORDER_END} оралиғида қабул қилинади.", 403)

    lines, qty_total, subtotal = [], 0, 0
    for it in body.get("items", []) or []:
        slot = str(it.get("slot"))
        try:
            qty = int(it.get("qty", 0))
        except (TypeError, ValueError):
            continue
        if slot in menu and 0 < qty <= 50:
            d = menu[slot]
            qty_total += qty
            subtotal += qty * d["price"]
            lines.append(f"• {html.escape(d['name'])} × {qty} = {som(qty * d['price'])}")
    if not lines:
        return json_err("Сават бўш ёки меню ўзгарган. Саҳифани янгиланг.")

    name = str(body.get("name", "")).strip()[:60]
    phone = str(body.get("phone", "")).strip()[:25]
    address = str(body.get("address", "")).strip()[:300]
    comment = str(body.get("comment", "")).strip()[:300]
    if not name or not address or len("".join(c for c in phone if c.isdigit())) < 9:
        return json_err("Исм, телефон ва манзилни тўлиқ киритинг.")

    fee = delivery_fee(qty_total)
    total = subtotal + fee
    no = next_order_no()
    uid = int(user["id"])
    uname = f" (@{html.escape(user['username'])})" if user.get("username") else ""

    text = (
        f"🍱 <b>Янги буюртма №{no}</b> · {datetime.now(TZ):%d.%m %H:%M}\n\n"
        + "\n".join(lines)
        + f"\n\nПорциялар: {qty_total} та\n"
        f"Етказиш: {'бепул' if fee == 0 else som(fee)}\n"
        f"<b>Жами: {som(total)}</b>\n\n"
        f"👤 <a href=\"tg://user?id={uid}\">{html.escape(name)}</a>{uname}\n"
        f"📞 {html.escape(phone)}\n"
        f"📍 {html.escape(address)}"
        + (f"\n💬 {html.escape(comment)}" if comment else "")
    )
    msg = await bot.send_message(GROUP_ID, text, reply_markup=order_kb(uid, no))

    loc = body.get("location") or {}
    try:
        lat, lon = float(loc["lat"]), float(loc["lon"])
        await bot.send_location(GROUP_ID, lat, lon, reply_to_message_id=msg.message_id)
    except (KeyError, TypeError, ValueError):
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("Lokatsiya yuborilmadi: %s", e)

    try:
        await bot.send_message(
            uid,
            f"Раҳмат! Буюртма №{no} қабул қилинди ✅\n\n" + "\n".join(lines)
            + f"\nЕтказиш: {'бепул' if fee == 0 else som(fee)}\n<b>Жами: {som(total)}</b>\n\n"
            "Тез орада оператор боғланади.",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Mijozga tasdiq yuborilmadi: %s", e)

    return web.json_response({"ok": True, "no": no, "total": total})


async def health(_req):
    return web.Response(text="ok")


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/menu", api_menu)
    app.router.add_post("/api/order", api_order)
    app.router.add_get("/photo/{slot}", photo)
    app.router.add_get("/health", health)
    if (ROOT / "static").is_dir():
        app.router.add_static("/static", ROOT / "static")
    if not USE_POLLING:
        SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
    return app


async def main():
    runner = web.AppRunner(build_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Server %s portda ishga tushdi", PORT)

    # "🍱 Меню" tugmasi BotFather orqali (/setmenubutton) o'rnatiladi — bu yerda so'rov yubormaymiz.
    if USE_POLLING:
        await safe_call("delete_webhook", bot.delete_webhook)
        await safe_call("load_menu", load_menu)
        await dp.start_polling(bot)
        return

    async def ensure_webhook():
        info = await bot.get_webhook_info()
        if info.url != BASE_URL + WEBHOOK_PATH:
            await bot.set_webhook(BASE_URL + WEBHOOK_PATH, secret_token=WEBHOOK_SECRET,
                                  allowed_updates=["message", "callback_query"])

    async def startup():
        await safe_call("webhook", ensure_webhook)
        await safe_call("load_menu", load_menu)

    asyncio.create_task(startup())
    await asyncio.Event().wait()


async def safe_call(name, fn, attempts: int = 12):
    """Telegram so'rovlari: limitga tushsa tobora uzoqroq kutib qayta urinadi, server yiqilmaydi."""
    wait = 0
    for i in range(attempts):
        try:
            await fn()
            log.info("%s: OK", name)
            return
        except TelegramRetryAfter as e:
            wait = min(max(wait * 2, e.retry_after + 5), 600)
            log.warning("%s: Telegram limiti, %s s kutamiz", name, wait)
            await asyncio.sleep(wait)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: xato (%s), qayta urinamiz", name, e)
            await asyncio.sleep(min(5 * (i + 1), 60))
    log.error("%s: bajarilmadi", name)


if __name__ == "__main__":
    asyncio.run(main())
