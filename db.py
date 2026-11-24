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
        _conn.commit()
    return _conn


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    d["items"] = json.loads(d.get("items") or "[]")
    d["status_label"] = STATUSES.get(d["status"], d["status"])
    return d


def create_order(now: datetime, **o) -> dict:
    c = conn()
    day = now.date().isoformat()
    no = c.execute("SELECT COUNT(*) FROM orders WHERE day=?", (day,)).fetchone()[0] + 1
    cur = c.execute(
        """INSERT INTO orders(no, day, created_at, updated_at, user_id, username, name, phone, address,
               comment, lat, lon, items, qty, subtotal, fee, total)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (no, day, now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"),
         o["user_id"], o.get("username"), o["name"], o["phone"], o["address"], o.get("comment", ""),
         o.get("lat"), o.get("lon"), json.dumps(o["items"], ensure_ascii=False),
         o["qty"], o["subtotal"], o["fee"], o["total"]),
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
        """SELECT COUNT(*) n, COALESCE(SUM(total),0) s, COALESCE(SUM(qty),0) q
           FROM orders WHERE day BETWEEN ? AND ? AND status!='cancelled'""",
        (day_from, day_to),
    ).fetchone()
    n, s = r["n"], r["s"]
    return {"orders": n, "revenue": s, "portions": r["q"], "avg": round(s / n) if n else 0}


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
