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

BOT_API_URL = os.environ.get("BOT_API_URL", "").rstrip("/")
CARD_NUMBER = os.environ.get("CARD_NUMBER", "").strip()   # masalan: 8600 1234 5678 9012
CARD_OWNER = os.environ.get("CARD_OWNER", "").strip()     # masalan: A. Valiyev  # o'zimizning Bot API server (ixtiyoriy)
_session = None
if BOT_API_URL:
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.client.telegram import TelegramAPIServer
    _session = AiohttpSession(api=TelegramAPIServer.from_base(BOT_API_URL))
bot = Bot(BOT_TOKEN, session=_session, default=DefaultBotProperties(parse_mode="HTML"))
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
    load_menu_local()


def load_menu_local():
    """Telegram'ga murojaat qilmasdan fayldan menyuni yuklaydi."""
    global menu
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
    "/tozala — бутун менюни тозалаш\n"
    "/panel — админ панелга кириш\n"
    "/hisob_nol — панелдаги буюртма ва статистикани 0 қилиш"
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


# ---------------- Bot: buyurtma holatlari (guruh tugmalari + panel) ----------------
import db  # noqa: E402

PANEL_IDS = ADMIN_IDS | {int(x) for x in os.environ.get("PANEL_IDS", "").replace(" ", "").split(",") if x}
PANEL_SECRET = hashlib.sha256(("panel:" + BOT_TOKEN).encode()).digest()

STATUS_ICON = {"new": "🆕", "accepted": "✅", "on_way": "🚚", "delivered": "📦", "cancelled": "❌"}
CUSTOMER_MSG = {
    "accepted": "✅ Буюртмангиз қабул қилинди! Тайёрланмоқда 🍱",
    "on_way": "🚚 Буюртмангиз йўлга чиқди! Тез орада етиб боради.",
    "delivered": "📦 Буюртмангиз етказилди. Ёқимли иштаҳа! 😊",
    "cancelled": "❌ Афсуски, буюртмангиз бекор қилинди. Саволлар бўлса, оператор билан боғланинг.",
}
NEXT_BTNS = {
    "new": [("accepted", "✅ Қабул қилиш"), ("cancelled", "❌ Бекор")],
    "accepted": [("on_way", "🚚 Йўлга чиқди"), ("cancelled", "❌ Бекор")],
    "on_way": [("delivered", "📦 Етказилди")],
}


PAY_ICON = {"pending": "⏳", "sent": "🧾", "paid": "✅", "rejected": "❌"}


def pay_line(o: dict) -> str:
    if o.get("payment") != "card":
        return "\n\n💵 <b>Тўлов: Нақд</b>"
    ps = o.get("pay_status") or "pending"
    t = f"\n\n💳 <b>Тўлов: Карта</b> — {PAY_ICON[ps]} {db.PAY_STATUSES[ps]}"
    if ps in ("paid", "rejected") and o.get("pay_by"):
        t += f" ({html.escape(o['pay_by'])})"
    return t


def receipt_kb(oid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Тўлов қабул қилинди", callback_data=f"pay:ok:{oid}"),
        InlineKeyboardButton(text="❌ Рад этиш", callback_data=f"pay:no:{oid}"),
    ]])


async def refresh_group_msg(o: dict):
    if o.get("group_msg_id"):
        try:
            await bot.edit_message_text(order_text(o), chat_id=GROUP_ID, message_id=o["group_msg_id"],
                                        reply_markup=order_kb(o))
        except Exception as e:  # noqa: BLE001
            log.warning("Guruh xabarini yangilab bo'lmadi: %s", e)


async def submit_receipt(o: dict, photo, by_name: str) -> None:
    """Chekni guruhga yuboradi va mijozga 'tekshirilmoqda' deb xabar beradi.
    photo — Telegram file_id yoki BufferedInputFile."""
    caption = (f"🧾 <b>Тўлов чеки — буюртма №{o['no']}</b>\n"
               f"Сумма: <b>{som(o['total'])}</b>\n👤 {html.escape(o['name'])} · {html.escape(o['phone'])}")
    msg_id, file_id = None, photo if isinstance(photo, str) else None
    try:
        kw = {"reply_to_message_id": o["group_msg_id"]} if o.get("group_msg_id") else {}
        msg = await bot.send_photo(GROUP_ID, photo, caption=caption, reply_markup=receipt_kb(o["id"]), **kw)
        msg_id, file_id = msg.message_id, msg.photo[-1].file_id
    except Exception as e:  # noqa: BLE001
        log.error("Chek guruhga yuborilmadi: %s", e)
    db.set_receipt(o["id"], file_id or "", msg_id)
    await refresh_group_msg(db.get_order(o["id"]))
    try:
        await bot.send_message(o["user_id"], f"🧾 Чекингиз қабул қилинди (буюртма №{o['no']}).\n"
                               "Тез орада тўловингиз ҳақида жавоб берамиз.")
    except Exception as e:  # noqa: BLE001
        log.warning("Mijozga yozib bo'lmadi: %s", e)


async def apply_pay(order_id: int, ok: bool, by: str) -> dict | None:
    o = db.get_order(order_id)
    if not o or o.get("payment") != "card":
        return None
    o = db.set_pay_status(order_id, "paid" if ok else "rejected", by)
    await refresh_group_msg(o)
    if o.get("receipt_msg_id"):
        try:
            await bot.edit_message_caption(
                chat_id=GROUP_ID, message_id=o["receipt_msg_id"],
                caption=(f"🧾 <b>Тўлов чеки — буюртма №{o['no']}</b>\nСумма: <b>{som(o['total'])}</b>\n\n"
                         + ("✅ Тўлов қабул қилинди" if ok else "❌ Рад этилди")
                         + f" — {html.escape(by)}, {datetime.now(TZ):%H:%M}"),
                reply_markup=None)
        except Exception as e:  # noqa: BLE001
            log.warning("Chek xabarini yangilab bo'lmadi: %s", e)
    text = (f"✅ Тўловингиз қабул қилинди! Раҳмат.\nБуюртма №{o['no']}" if ok else
            f"❌ Буюртма №{o['no']} бўйича тўлов тасдиқланмади.\n"
            "Илтимос, тўғри чек скриншотини шу ерга юборинг ёки оператор билан боғланинг.")
    try:
        await bot.send_message(o["user_id"], text)
    except Exception as e:  # noqa: BLE001
        log.warning("Mijozga yozib bo'lmadi: %s", e)
    return o


@router.callback_query(F.data.startswith("pay:"))
async def on_pay_cb(cq: CallbackQuery):
    _, act, oid = cq.data.split(":")
    o = await apply_pay(int(oid), act == "ok", cq.from_user.full_name)
    await cq.answer(("Тўлов тасдиқланди" if act == "ok" else "Рад этилди") if o else "Буюртма топилмади")


async def _awaits_receipt(m: Message) -> dict | bool:
    if not m.from_user or not m.photo or m.chat.type != "private":
        return False
    o = db.awaiting_receipt(m.from_user.id)
    return {"rcpt_order": o} if o else False


@dp.message(_awaits_receipt)  # mijoz chekni bot chatiga rasm qilib yuborsa
async def on_receipt_photo(m: Message, rcpt_order: dict):
    await submit_receipt(rcpt_order, m.photo[-1].file_id, m.from_user.full_name)


def order_kb(o: dict) -> InlineKeyboardMarkup | None:
    btns = NEXT_BTNS.get(o["status"])
    if not btns:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t, callback_data=f"st:{st}:{o['id']}") for st, t in btns
    ]])


def order_items_lines(o: dict) -> list[str]:
    return [f"• {html.escape(i['name'])} × {i['qty']} = {som(i['qty'] * i['price'])}" for i in o["items"]]


def order_text(o: dict) -> str:
    created = datetime.fromisoformat(o["created_at"])
    uname = f" (@{html.escape(o['username'])})" if o.get("username") else ""
    t = (
        f"🍱 <b>Буюртма №{o['no']}</b> · {created:%d.%m %H:%M}\n\n"
        + "\n".join(order_items_lines(o))
        + f"\n\nПорциялар: {o['qty']} та\n"
        f"Етказиш: {'бепул' if o['fee'] == 0 else som(o['fee'])}\n"
        f"<b>Жами: {som(o['total'])}</b>\n\n"
        f"👤 <a href=\"tg://user?id={o['user_id']}\">{html.escape(o['name'])}</a>{uname}\n"
        f"📞 {html.escape(o['phone'])}\n"
        f"📍 {html.escape(o['address'])}"
        + (f"\n💬 {html.escape(o['comment'])}" if o.get("comment") else "")
        + pay_line(o)
    )
    if o["status"] != "new":
        upd = datetime.fromisoformat(o["updated_at"])
        t += (f"\n\n{STATUS_ICON[o['status']]} <b>{db.STATUSES[o['status']]}</b>"
              f" — {html.escape(o.get('status_by') or '')}, {upd:%H:%M}")
    return t


async def apply_status(order_id: int, status: str, by: str) -> dict | None:
    """Holatni bazada o'zgartiradi, guruhdagi xabarni yangilaydi va mijozga xabar beradi."""
    old = db.get_order(order_id)
    if not old or old["status"] == status:
        return old
    o = db.set_status(order_id, status, by, datetime.now(TZ))
    await refresh_group_msg(o)
    if status in CUSTOMER_MSG:
        try:
            await bot.send_message(o["user_id"], f"{CUSTOMER_MSG[status]}\nБуюртма №{o['no']}")
        except Exception as e:  # noqa: BLE001
            log.warning("Mijozga yozib bo'lmadi: %s", e)
    return o


OLD_STATUS = {"ok": ("✅ Қабул қилинди", "✅ Буюртмангиз қабул қилинди! Тайёрланмоқда 🍱"),
              "go": ("🚚 Йўлга чиқди", "🚚 Буюртмангиз йўлга чиқди! Тез орада етиб боради.")}


@router.callback_query(F.data.startswith("st:"))
async def on_status(cq: CallbackQuery):
    parts = cq.data.split(":")
    if len(parts) == 4:  # panelgacha bo'lgan eski xabarlar
        _, step, uid, no = parts
        label, customer_text = OLD_STATUS[step]
        try:
            await bot.send_message(int(uid), f"{customer_text}\nБуюртма №{no}")
        except Exception as e:  # noqa: BLE001
            log.warning("Mijozga yozib bo'lmadi: %s", e)
        new_text = f"{cq.message.html_text}\n\n{label} — {html.escape(cq.from_user.full_name)}, {datetime.now(TZ):%H:%M}"
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text=OLD_STATUS["go"][0], callback_data=f"st:go:{uid}:{no}")]]) if step == "ok" else None
        try:
            await cq.message.edit_text(new_text, reply_markup=kb)
        except TelegramBadRequest as e:
            log.warning("Xabarni tahrirlab bo'lmadi: %s", e)
        return await cq.answer(label)
    _, status, oid = parts
    o = await apply_status(int(oid), status, cq.from_user.full_name)
    await cq.answer(db.STATUSES.get(status, "") if o else "Буюртма топилмади")


# ---------------- Bot: statistikani nolga tushirish (faqat ADMIN_IDS) ----------------
@dp.message(Command("hisob_nol"), F.chat.type == "private")
async def on_wipe(m: Message):
    if not m.from_user or m.from_user.id not in ADMIN_IDS:
        return
    n = db.count_orders()
    if not n:
        return await m.answer("Bazada buyurtma yo'q — hisoblar allaqachon 0.")
    await m.answer(
        f"⚠️ Panel'dagi <b>{n} ta</b> buyurtma va butun statistika o'chiriladi, raqamlash №1 dan boshlanadi.\n"
        "Buni <b>qaytarib bo'lmaydi</b>. Davom etamizmi?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Ha, o'chirish", callback_data="wipe:yes"),
            InlineKeyboardButton(text="Bekor qilish", callback_data="wipe:no"),
        ]]),
    )


@dp.callback_query(F.data.startswith("wipe:"))
async def on_wipe_cb(cq: CallbackQuery):
    if cq.from_user.id not in ADMIN_IDS:
        return await cq.answer("Ruxsat yo'q", show_alert=True)
    if cq.data == "wipe:yes":
        n = db.wipe_orders()
        log.info("Buyurtmalar bazasi tozalandi: %s ta (%s)", n, cq.from_user.id)
        await cq.message.edit_text(f"✅ {n} ta buyurtma o'chirildi. Panel'dagi hisoblar endi 0.")
    else:
        await cq.message.edit_text("Bekor qilindi, hech narsa o'chirilmadi.")
    await cq.answer()


# ---------------- Bot: admin panelga kirish ----------------
def panel_token(uid: int, ttl: int = 600) -> str:
    exp = int(time.time()) + ttl
    sig = hmac.new(PANEL_SECRET, f"login:{uid}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{uid}.{exp}.{sig}"


@dp.message(Command("panel"), F.chat.type == "private")  # dp: umumiy handlerlardan oldin ishlaydi
async def on_panel(m: Message):
    if not m.from_user or m.from_user.id not in PANEL_IDS:
        return await m.answer("Panelga kirish huquqingiz yo'q. ID'ingiz: "
                              f"<code>{m.from_user.id if m.from_user else '?'}</code> — buni administratorga yuboring.")
    url = f"{BASE_URL}/admin/login?t={panel_token(m.from_user.id)}"
    await m.answer(
        "🔐 Admin panelga kirish havolasi (10 daqiqa amal qiladi, bir marta kirsangiz brauzer 30 kun eslab qoladi).\n"
        "Havolani hech kimga yubormang.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📊 Panelni ochish", url=url)]]),
    )


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
         "fee": DELIVERY_FEE, "freeFrom": FREE_FROM,
         "card": {"number": CARD_NUMBER, "owner": CARD_OWNER} if CARD_NUMBER else None},
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


def pay_note(payment: str, total: int) -> str:
    if payment != "card":
        return "💵 Тўлов: нақд — курьерга берасиз.\n"
    if CARD_NUMBER:
        owner = f" ({html.escape(CARD_OWNER)})" if CARD_OWNER else ""
        return (f"💳 Тўлов: карта орқали.\n<b>{som(total)}</b> ни <code>{html.escape(CARD_NUMBER)}</code>{owner} "
                "картасига ўтказинг ва чек скриншотини шу чатга юборинг.\n")
    return "💳 Тўлов: карта орқали — оператор карта рақамини юборади, кейин чек скриншотини шу чатга юборасиз.\n"


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

    lines, items, qty_total, subtotal = [], [], 0, 0
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
            items.append({"slot": slot, "name": d["name"], "price": d["price"], "qty": qty})
    if not lines:
        return json_err("Сават бўш ёки меню ўзгарган. Саҳифани янгиланг.")

    name = str(body.get("name", "")).strip()[:60]
    phone = str(body.get("phone", "")).strip()[:25]
    address = str(body.get("address", "")).strip()[:300]
    comment = str(body.get("comment", "")).strip()[:300]
    payment = "card" if body.get("payment") == "card" else "cash"
    if not name or not address or len("".join(c for c in phone if c.isdigit())) < 9:
        return json_err("Исм, телефон ва манзилни тўлиқ киритинг.")

    fee = delivery_fee(qty_total)
    total = subtotal + fee
    uid = int(user["id"])
    loc = body.get("location") or {}
    try:
        lat, lon = float(loc["lat"]), float(loc["lon"])
    except (KeyError, TypeError, ValueError):
        lat = lon = None
    o = db.create_order(
        datetime.now(TZ), user_id=uid, username=user.get("username"), name=name, phone=phone,
        address=address, comment=comment, lat=lat, lon=lon, items=items,
        qty=qty_total, subtotal=subtotal, fee=fee, total=total, payment=payment,
    )
    no = o["no"]

    try:
        msg = await bot.send_message(GROUP_ID, order_text(o), reply_markup=order_kb(o))
        db.set_group_msg(o["id"], msg.message_id)
        if lat is not None:
            try:
                await bot.send_location(GROUP_ID, lat, lon, reply_to_message_id=msg.message_id)
            except Exception as e:  # noqa: BLE001
                log.warning("Lokatsiya yuborilmadi: %s", e)
    except Exception as e:  # noqa: BLE001
        log.error("Buyurtma guruhga yuborilmadi (panelda bor): %s", e)

    try:
        await bot.send_message(
            uid,
            f"Раҳмат! Буюртма №{no} қабул қилинди ✅\n\n" + "\n".join(lines)
            + f"\nЕтказиш: {'бепул' if fee == 0 else som(fee)}\n<b>Жами: {som(total)}</b>\n\n"
            + pay_note(payment, total) + "Тез орада оператор боғланади.",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Mijozga tasdiq yuborilmadi: %s", e)

    return web.json_response({"ok": True, "no": no, "id": o["id"], "total": total, "payment": payment})


async def health(_req):
    return web.Response(text="ok")


async def api_receipt(req):
    try:
        data = await req.post()
    except Exception:  # noqa: BLE001
        return json_err("Файл юкланмади")
    user = check_init_data(str(data.get("initData", "")))
    if not user:
        return json_err("Илтимос, менюни Telegram бот орқали очинг.", 401)
    try:
        o = db.get_order(int(data.get("order_id", 0)))
    except (TypeError, ValueError):
        o = None
    if not o or o["user_id"] != int(user["id"]) or o.get("payment") != "card":
        return json_err("Буюртма топилмади", 404)
    if o.get("pay_status") == "paid":
        return json_err("Бу буюртма тўлови аллақачон тасдиқланган.")
    f = data.get("file")
    if not hasattr(f, "file"):
        return json_err("Скриншотни танланг")
    raw = f.file.read()
    if not raw or len(raw) > 8 * 1024 * 1024:
        return json_err("Расм ҳажми катта ёки бўш")
    from aiogram.types import BufferedInputFile
    await submit_receipt(o, BufferedInputFile(raw, filename="chek.jpg"), user.get("first_name", ""))
    return web.json_response({"ok": True})


def build_app() -> web.Application:
    app = web.Application(client_max_size=10 * 1024 * 1024)
    app.router.add_post("/api/receipt", api_receipt)
    app.router.add_get("/", index)
    app.router.add_get("/api/menu", api_menu)
    app.router.add_post("/api/order", api_order)
    app.router.add_get("/photo/{slot}", photo)
    app.router.add_get("/health", health)
    import admin
    admin.setup(app, PANEL_IDS, PANEL_SECRET, apply_status, apply_pay, bot,
                lambda: [menu[k] for k in sorted(menu, key=lambda x: int(x) if str(x).isdigit() else 99)])
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
        # setWebhook/deleteWebhook chaqirilmaydi — faqat getUpdates.
        load_menu_local()
        asyncio.create_task(safe_call("load_menu", load_menu))
        while True:
            try:
                await dp.start_polling(bot, handle_signals=False)
            except TelegramRetryAfter as e:
                log.warning("polling: Telegram limiti, %s s | %s", e.retry_after, e.message)
                await asyncio.sleep(e.retry_after + 5)
            except Exception as e:  # noqa: BLE001
                log.warning("polling xatosi: %s", e)
                await asyncio.sleep(15)

    async def ensure_webhook():
        await bot.set_webhook(BASE_URL + WEBHOOK_PATH, secret_token=WEBHOOK_SECRET,
                                  allowed_updates=["message", "callback_query"])

    async def startup():
        await safe_call("webhook", ensure_webhook)
        await safe_call("load_menu", load_menu)

    load_menu_local()
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
            log.warning("%s: Telegram limiti, %s s kutamiz | %s", name, wait, e.message)
            await asyncio.sleep(wait)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: xato (%s), qayta urinamiz", name, e)
            await asyncio.sleep(min(5 * (i + 1), 60))
    log.error("%s: bajarilmadi", name)


if __name__ == "__main__":
    asyncio.run(main())
