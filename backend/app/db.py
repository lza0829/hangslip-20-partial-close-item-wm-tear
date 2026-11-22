from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime

from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  rack_count INTEGER NOT NULL DEFAULT 2,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS hang_tickets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_no TEXT NOT NULL UNIQUE,
  store_code TEXT NOT NULL,
  pickup_code TEXT NOT NULL,
  customer_name TEXT NOT NULL,
  status TEXT NOT NULL,
  opened_at TEXT NOT NULL,
  due_date TEXT NOT NULL,
  closed_at TEXT,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ticket_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_no TEXT NOT NULL,
  garment TEXT NOT NULL,
  qty INTEGER NOT NULL,
  remark TEXT NOT NULL DEFAULT '',
  picked_qty INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS process_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_no TEXT NOT NULL,
  from_status TEXT,
  to_status TEXT NOT NULL,
  operator TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT ''
);
-- 历史表：一票一行，仅在全票取齐关票时写入（供旧版核对）。
CREATE TABLE IF NOT EXISTS pickups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_no TEXT NOT NULL UNIQUE,
  pickup_code TEXT NOT NULL,
  picked_at TEXT NOT NULL,
  operator TEXT NOT NULL
);
-- 核销回执：每一次取衣（整票或按件分取）各留一张回执。
CREATE TABLE IF NOT EXISTS pickup_receipts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_no TEXT NOT NULL,
  receipt_no TEXT NOT NULL UNIQUE,
  pickup_code TEXT NOT NULL,
  picked_at TEXT NOT NULL,
  operator TEXT NOT NULL,
  partial INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pickup_receipts_ticket ON pickup_receipts(ticket_no);
-- 回执行：按件记账，是件数守恒重放的唯一凭据。
CREATE TABLE IF NOT EXISTS pickup_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  receipt_id INTEGER NOT NULL,
  ticket_no TEXT NOT NULL,
  item_id INTEGER NOT NULL,
  qty INTEGER NOT NULL,
  picked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pickup_lines_ticket ON pickup_lines(ticket_no);
CREATE INDEX IF NOT EXISTS idx_pickup_lines_item ON pickup_lines(item_id);
CREATE TABLE IF NOT EXISTS quarantines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_no TEXT NOT NULL,
  kind TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  resolved INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS day_gates (
  day TEXT PRIMARY KEY,
  sealed INTEGER NOT NULL DEFAULT 0,
  sealed_at TEXT,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watermarks (
  key TEXT PRIMARY KEY,
  value INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);
"""

STATUSES = (
    "已收",
    "水洗",
    "烘干",
    "熨烫",
    "待取",
    "部分已取",
    "已取",
    "超期隔离",
    "丢失隔离",
)
# 票态语义（全局唯一口径）：
#   待取      —— 全部在册衣物件未取，等取。
#   部分已取  —— 有件已取走、仍有件在册未取；仍属在途，不得视作已取出途。
#   已取      —— 全部在册衣物件取齐，票关闭。
# 部分已取 与 待取 一样可继续取件；两者均不可再推进工序。
FORWARD = {
    "已收": ("水洗", "烘干", "熨烫", "待取"),
    "水洗": ("烘干", "熨烫", "待取"),
    "烘干": ("熨烫", "待取"),
    "熨烫": ("待取",),
    "待取": (),
    "部分已取": (),
}
# 仍在途（未关票、未丢失）的票态；水位按此统计。
IN_FLIGHT_STATUSES = ("已收", "水洗", "烘干", "熨烫", "待取", "部分已取")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def transaction():
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """旧库补列与假分取收口。

    全局唯一口径：票籍听件——ticket_items 的勾选位图（picked_qty/qty）
    是唯一事实源，挂牌态与水位都必须能由它重放。历史上存在过一层
    「假分取」旁路：只把票态刷成已取、水位减一，却不动件位。这里把
    被它撕裂的库按位图校正回来。
    """
    cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(ticket_items)").fetchall()
    }
    if "picked_qty" not in cols:
        conn.execute(
            "ALTER TABLE ticket_items ADD COLUMN picked_qty INTEGER NOT NULL DEFAULT 0"
        )

    # 先收口假分取：按真实勾选位图把被毒成「已取」的票修回待取/部分已取，
    # 必须排在旧版「已取即全取齐」兜底之前，否则残留件会被兜底回填掩盖。
    _heal_poisoned_partials(conn)

    # 存量真「已取」票视作全票取齐，并补一张整票回执，使件数守恒可重放。
    conn.execute(
        """UPDATE ticket_items SET picked_qty=qty
           WHERE ticket_no IN (SELECT ticket_no FROM hang_tickets WHERE status='已取')
             AND picked_qty=0"""
    )
    legacy = conn.execute(
        """SELECT t.ticket_no, t.pickup_code, COALESCE(t.closed_at, t.opened_at) AS ts
             FROM hang_tickets t
            WHERE t.status='已取'
              AND NOT EXISTS (SELECT 1 FROM pickup_receipts r WHERE r.ticket_no=t.ticket_no)"""
    ).fetchall()
    for t in legacy:
        cur = conn.execute(
            """INSERT INTO pickup_receipts
               (ticket_no, receipt_no, pickup_code, picked_at, operator, partial, note)
               VALUES (?,?,?,?,?,0,?)""",
            (t["ticket_no"], f"{t['ticket_no']}-回执01", t["pickup_code"], t["ts"], "迁移", "存量整票补齐"),
        )
        conn.execute(
            """INSERT INTO pickup_lines(receipt_id, ticket_no, item_id, qty, picked_at)
               SELECT ?, ticket_no, id, picked_qty, ?
                 FROM ticket_items
                WHERE ticket_no=? AND picked_qty>0""",
            (cur.lastrowid, t["ts"], t["ticket_no"]),
        )


def _heal_poisoned_partials(conn: sqlite3.Connection) -> None:
    """按衣物件勾选位图重放票态，收口假分取留下的撕裂。

    规则（只信件位，不信挂牌态）：
      残留为 0  → 必须是「已取」关票，补齐 closed_at；
      残留 > 0 且挂牌却是「已取」→ 假关票：有已取件落「部分已取」，
                  一件未取落「待取」，清 closed_at；
      隔离态保留（隔离票上的未取件仍在残留区可核）。
    随后清掉假分取缓存键，并按重放后的票态重算在途水位。
    """
    conn.execute("DELETE FROM settings WHERE key LIKE 'partial_surface%'")

    rows = conn.execute(
        """
        SELECT t.ticket_no, t.status, t.closed_at,
               COALESCE(SUM(i.qty), 0) AS total_qty,
               COALESCE(SUM(i.picked_qty), 0) AS picked_qty,
               COALESCE(SUM(i.qty - i.picked_qty), 0) AS remaining_qty
          FROM hang_tickets t
          LEFT JOIN ticket_items i ON i.ticket_no = t.ticket_no
         GROUP BY t.ticket_no
        """
    ).fetchall()
    now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
    # 取衣三态可由件位重放；工序态与隔离态保持不动。
    projected = ("待取", "部分已取", "已取")
    for r in rows:
        ticket_no, status = r["ticket_no"], r["status"]
        remaining, picked, total = r["remaining_qty"], r["picked_qty"], r["total_qty"]
        if status not in projected:
            continue
        if remaining > 0:
            # 有残留就绝不能是关票态：有已取件落部分已取，一件未取落待取。
            target = "部分已取" if picked > 0 else "待取"
        elif total > 0:
            target = "已取"
        else:
            target = status
        if target == status:
            continue
        if target == "已取":
            conn.execute(
                "UPDATE hang_tickets SET status='已取', closed_at=COALESCE(closed_at, ?) "
                "WHERE ticket_no=?",
                (now, ticket_no),
            )
        else:
            conn.execute(
                "UPDATE hang_tickets SET status=?, closed_at=NULL WHERE ticket_no=?",
                (target, ticket_no),
            )
        conn.execute(
            """INSERT INTO process_events(ticket_no, from_status, to_status, operator, occurred_at, note)
               VALUES (?,?,?,?,?,?)""",
            (ticket_no, status, target, "迁移校正", now, "按勾选位图收口假分取"),
        )

    placeholders = ",".join("?" for _ in IN_FLIGHT_STATUSES)
    n = conn.execute(
        f"SELECT COUNT(*) AS c FROM hang_tickets WHERE status IN ({placeholders})",
        IN_FLIGHT_STATUSES,
    ).fetchone()["c"]
    # 仅在水位行已存在（存量库）时重算；全新库的首条水位由种子写入，
    # 否则种子的裸 INSERT 会撞 watermarks 唯一键。
    if conn.execute("SELECT 1 FROM watermarks WHERE key='在途票'").fetchone():
        conn.execute(
            "UPDATE watermarks SET value=?, updated_at=? WHERE key='在途票'",
            (n, now),
        )


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return dict(row)
