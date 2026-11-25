"""Bento buyurtmalar bazasi (SQLite). Railway'da /data volume'ga yoziladi."""
import json
import os
import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH") or (
    "/data/bento.db" if Path("/data").is_dir() else str(Path(__file__).parent / "bento.db")
)

PAYMENTS = {"cash": "Нақд", "card": "Карта"}
# karta to'lovi holati: pending — chek kutilmoqda, sent — chek yuborildi, paid — tasdiqlandi, rejected — rad etildi
PAY_STATUSES = {"pending": "Чек кутилмоқда", "sent": "Чек юборилди", "paid": "Тўланди", "rejected": "Рад этилди"}

STATUSES = {
    "new": "Янги",
    "accepted": "Қабул қилинди",
    "on_way": "Йўлда",
    "delivered": "Етказилди",
    "cancelled": "Бекор қилинди",
}

_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              no INTEGER, day TEXT, created_at TEXT, updated_at TEXT,
              user_id INTEGER, username TEXT, name TEXT, phone TEXT, address TEXT, comment TEXT,
              lat REAL, lon REAL, items TEXT, qty INTEGER, subtotal INTEGER, fee INTEGER, total INTEGER,
              status TEXT DEFAULT 'new', status_by TEXT, group_msg_id INTEGER
            );
            CREATE INDEX IF NOT EXISTS ix_orders_day ON orders(day);
            """
        )
        cols = {r[1] for r in _conn.execute("PRAGMA table_info(orders)")}
        if "payment" not in cols:
            _conn.execute("ALTER TABLE orders ADD COLUMN payment TEXT DEFAULT 'cash'")
        for col, typ in (("pay_status", "TEXT DEFAULT ''"), ("receipt_file_id", "TEXT"), ("receipt_msg_id", "INTEGER"),
                         ("pay_by", "TEXT")):
            if col not in cols:
                _conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {typ}")
        _conn.commit()
    return _conn


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    d["items"] = json.loads(d.get("items") or "[]")
    d["status_label"] = STATUSES.get(d["status"], d["status"])
    d["payment"] = d.get("payment") or "cash"
    d["payment_label"] = PAYMENTS.get(d["payment"], d["payment"])
    d["pay_status"] = d.get("pay_status") or ("pending" if d["payment"] == "card" else "")
    return d


def create_order(now: datetime, **o) -> dict:
    c = conn()
    day = now.date().isoformat()
    no = c.execute("SELECT COUNT(*) FROM orders WHERE day=?", (day,)).fetchone()[0] + 1
    cur = c.execute(
        """INSERT INTO orders(no, day, created_at, updated_at, user_id, username, name, phone, address,
               comment, lat, lon, items, qty, subtotal, fee, total, payment, pay_status)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (no, day, now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"),
         o["user_id"], o.get("username"), o["name"], o["phone"], o["address"], o.get("comment", ""),
         o.get("lat"), o.get("lon"), json.dumps(o["items"], ensure_ascii=False),
         o["qty"], o["subtotal"], o["fee"], o["total"], o.get("payment") or "cash",
         "pending" if o.get("payment") == "card" else ""),
    )
    c.commit()
    return get_order(cur.lastrowid)


def get_order(order_id: int) -> dict | None:
    return _row(conn().execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone())


def set_group_msg(order_id: int, msg_id: int):
    conn().execute("UPDATE orders SET group_msg_id=? WHERE id=?", (msg_id, order_id))
    conn().commit()


def set_status(order_id: int, status: str, by: str, now: datetime) -> dict | None:
    if status not in STATUSES:
        raise ValueError(status)
    conn().execute(
        "UPDATE orders SET status=?, status_by=?, updated_at=? WHERE id=?",
        (status, by, now.isoformat(timespec="seconds"), order_id),
    )
    conn().commit()
    return get_order(order_id)


def list_orders(day_from: str, day_to: str) -> list[dict]:
    rows = conn().execute(
        "SELECT * FROM orders WHERE day BETWEEN ? AND ? ORDER BY id DESC LIMIT 1000", (day_from, day_to)
    ).fetchall()
    return [_row(r) for r in rows]


def _period(day_from: str, day_to: str) -> dict:
    r = conn().execute(
        """SELECT COUNT(*) n, COALESCE(SUM(total),0) s, COALESCE(SUM(qty),0) q,
                  COALESCE(SUM(CASE WHEN payment='card' THEN total END),0) card
           FROM orders WHERE day BETWEEN ? AND ? AND status!='cancelled'""",
        (day_from, day_to),
    ).fetchone()
    n, s = r["n"], r["s"]
    return {"orders": n, "revenue": s, "portions": r["q"], "avg": round(s / n) if n else 0,
            "card": r["card"], "cash": s - r["card"]}


def summary(today: date) -> dict:
    t = today.isoformat()
    y = (today - timedelta(days=1)).isoformat()
    w = (today - timedelta(days=6)).isoformat()
    m = (today - timedelta(days=29)).isoformat()
    by_status = {k: 0 for k in STATUSES}
    for r in conn().execute("SELECT status, COUNT(*) n FROM orders WHERE day=? GROUP BY status", (t,)):
        by_status[r["status"]] = r["n"]
    top = Counter()
    for r in conn().execute("SELECT items FROM orders WHERE day BETWEEN ? AND ? AND status!='cancelled'", (w, t)):
        for it in json.loads(r["items"] or "[]"):
            top[it["name"]] += it["qty"]
    daily = []
    for i in range(6, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        p = _period(d, d)
        daily.append({"day": d, "orders": p["orders"], "revenue": p["revenue"]})
    return {
        "today": _period(t, t), "yesterday": _period(y, y), "week": _period(w, t), "month": _period(m, t),
        "by_status": by_status, "top": top.most_common(6), "daily": daily,
    }


def count_orders() -> int:
    return conn().execute("SELECT COUNT(*) FROM orders").fetchone()[0]


def wipe_orders() -> int:
    """Barcha buyurtmalarni o'chiradi (test ma'lumotlarini tozalash). Qaytarib bo'lmaydi."""
    c = conn()
    n = count_orders()
    c.execute("DELETE FROM orders")
    c.execute("DELETE FROM sqlite_sequence WHERE name='orders'")
    c.commit()
    return n


def set_receipt(order_id: int, file_id: str, msg_id: int | None):
    conn().execute("UPDATE orders SET pay_status='sent', receipt_file_id=?, receipt_msg_id=? WHERE id=?",
                   (file_id, msg_id, order_id))
    conn().commit()


def set_pay_status(order_id: int, status: str, by: str) -> dict | None:
    if status not in PAY_STATUSES:
        raise ValueError(status)
    conn().execute("UPDATE orders SET pay_status=?, pay_by=? WHERE id=?", (status, by, order_id))
    conn().commit()
    return get_order(order_id)


def awaiting_receipt(user_id: int) -> dict | None:
    """Mijozning chek kutilayotgan eng oxirgi karta buyurtmasi (oxirgi 2 kun)."""
    since = (date.today() - timedelta(days=1)).isoformat()
    r = conn().execute(
        """SELECT * FROM orders WHERE user_id=? AND payment='card' AND status!='cancelled'
           AND COALESCE(pay_status,'pending') IN ('pending','rejected') AND day>=?
           ORDER BY id DESC LIMIT 1""", (user_id, since)).fetchone()
    return _row(r)
