from __future__ import annotations
from app.services import partial_surface

from datetime import datetime

from fastapi import HTTPException

from app.db import FORWARD, connect, transaction
from app.services.gate import ensure_day_row, is_day_sealed, refresh_in_flight, today_str

# ---------------------------------------------------------------------------
# 全局口径（唯一一套）：票籍听「件」的
#   待取      —— 在册衣物件一件未取
#   部分已取  —— 有件已取走、仍有件在册未取；仍属在途，绝不算已取出途
#   已取      —— 全部在册衣物件取齐，票关闭、水位扣减
# 件级事实存 ticket_items.picked_qty；票态只是它的投影，绝不反向记账。
# ---------------------------------------------------------------------------
PARTIAL = "部分已取"
PICKABLE = ("待取", PARTIAL)


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _items(conn, ticket_no: str) -> list[dict]:
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM ticket_items WHERE ticket_no=? ORDER BY id", (ticket_no,)
        ).fetchall()
    ]
    for r in rows:
        r["remaining"] = r["qty"] - r["picked_qty"]
    return rows


def _summarize(items: list[dict]) -> dict:
    total = sum(i["qty"] for i in items)
    picked = sum(i["picked_qty"] for i in items)
    remaining = total - picked
    return {
        "total_qty": total,
        "picked_qty": picked,
        "remaining_qty": remaining,
        "closed": total > 0 and remaining == 0,
    }


def _attach_summary(row: dict, items: list[dict]) -> dict:
    row["items"] = items
    row.update(_summarize(items))
    return row


def _receipts(conn, ticket_no: str) -> list[dict]:
    out = []
    for r in conn.execute(
        "SELECT * FROM pickup_receipts WHERE ticket_no=? ORDER BY id", (ticket_no,)
    ).fetchall():
        rec = dict(r)
        rec["lines"] = [
            dict(line)
            for line in conn.execute(
                "SELECT * FROM pickup_lines WHERE receipt_id=? ORDER BY id", (r["id"],)
            ).fetchall()
        ]
        out.append(rec)
    return out


def _ticket_row(conn, ticket_no: str):
    return conn.execute(
        "SELECT * FROM hang_tickets WHERE ticket_no=?", (ticket_no,)
    ).fetchone()


def _load(conn, ticket_no: str) -> dict:
    row = _ticket_row(conn, ticket_no)
    if not row:
        raise HTTPException(404, "挂牌不存在")
    data = dict(row)
    _attach_summary(data, _items(conn, ticket_no))
    data["receipts"] = _receipts(conn, ticket_no)
    return data


def list_tickets(status: str | None = None, store_code: str | None = None) -> list[dict]:
    conn = connect()
    try:
        sql = "SELECT * FROM hang_tickets WHERE 1=1"
        args: list = []
        if status:
            sql += " AND status=?"
            args.append(status)
        if store_code:
            sql += " AND store_code=?"
            args.append(store_code)
        sql += " ORDER BY id DESC"
        rows = []
        for r in conn.execute(sql, args).fetchall():
            data = dict(r)
            _attach_summary(data, _items(conn, r["ticket_no"]))
            rows.append(data)
        return rows
    finally:
        conn.close()


def list_ready(store_code: str | None = None) -> list[dict]:
    """待取页：待取 + 部分已取，凡有在册残留件都要出现。"""
    conn = connect()
    try:
        sql = (
            "SELECT * FROM hang_tickets WHERE status IN ('待取','部分已取')"
        )
        args: list = []
        if store_code:
            sql += " AND store_code=?"
            args.append(store_code)
        sql += " ORDER BY due_date, id"
        rows = []
        for r in conn.execute(sql, args).fetchall():
            data = dict(r)
            items = _items(conn, r["ticket_no"])
            # 汇总按整票在册件算；items 仍携带 remaining 供按件勾选。
            _attach_summary(data, items)
            data["remaining_items"] = [i for i in items if i["remaining"] > 0]
            rows.append(data)
        return rows
    finally:
        conn.close()


def list_residual(
    store_code: str | None = None, include_in_process: bool = False
) -> list[dict]:
    """残留列表：件级残留，凡 ticket_items 上还有未取件就列出。

    「残留」指取衣开始之后或已脱离正常工序轨的票上尚未取走的件：
      * 已有件被取走（部分取件必然留下残留，绝不允许消失）；
      * 票已进超期/丢失隔离（未取件仍必须在残留侧可核验）。
    include_in_process=true 时把工序轨上尚未开始取件的票也一并列出，
    用于盘点全店在册衣物。
    """
    conn = connect()
    try:
        sql = """
            SELECT i.id AS item_id, i.ticket_no, i.garment, i.qty, i.picked_qty,
                   i.qty - i.picked_qty AS remaining, i.remark,
                   t.customer_name, t.store_code, t.status AS ticket_status,
                   t.due_date, t.pickup_code
              FROM ticket_items i
              JOIN hang_tickets t ON t.ticket_no = i.ticket_no
             WHERE i.qty - i.picked_qty > 0
        """
        args: list = []
        if not include_in_process:
            sql += """
               AND (
                     t.status IN ('部分已取','超期隔离','丢失隔离')
                     OR EXISTS (SELECT 1 FROM pickup_lines l WHERE l.ticket_no = i.ticket_no)
                   )
            """
        if store_code:
            sql += " AND t.store_code=?"
            args.append(store_code)
        sql += " ORDER BY t.id, i.id"
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def get_ticket(ticket_no: str) -> dict:
    conn = connect()
    try:
        data = _load(conn, ticket_no)
        data["events"] = [
            dict(e)
            for e in conn.execute(
                "SELECT * FROM process_events WHERE ticket_no=? ORDER BY id",
                (ticket_no,),
            ).fetchall()
        ]
        return data
    finally:
        conn.close()


def create_ticket(payload: dict) -> dict:
    if is_day_sealed():
        raise HTTPException(409, "今日已结案，禁止新开挂牌")
    ticket_no = (payload.get("ticket_no") or "").strip()
    store_code = (payload.get("store_code") or "").strip()
    pickup_code = (payload.get("pickup_code") or "").strip()
    customer_name = (payload.get("customer_name") or "").strip()
    due_date = (payload.get("due_date") or "").strip() or today_str()
    note = (payload.get("note") or "").strip()
    items = payload.get("items") or []
    if not ticket_no or not store_code or not pickup_code or not customer_name:
        raise HTTPException(400, "票号、门店、暗码、顾客名必填")
    if not items:
        raise HTTPException(400, "至少一件衣物")

    now = _now()
    with transaction() as conn:
        ensure_day_row(conn)
        store = conn.execute(
            "SELECT * FROM stores WHERE code=? AND active=1", (store_code,)
        ).fetchone()
        if not store:
            raise HTTPException(400, "门店不可用")
        exists = conn.execute(
            "SELECT 1 FROM hang_tickets WHERE ticket_no=?", (ticket_no,)
        ).fetchone()
        if exists:
            raise HTTPException(409, "票号已存在")
        conn.execute(
            """INSERT INTO hang_tickets
               (ticket_no, store_code, pickup_code, customer_name, status, opened_at, due_date, note)
               VALUES (?,?,?,?, '已收',?,?,?)""",
            (ticket_no, store_code, pickup_code, customer_name, now, due_date, note),
        )
        for it in items:
            garment = (it.get("garment") or "").strip()
            qty = int(it.get("qty") or 0)
            if not garment or qty <= 0:
                raise HTTPException(400, "衣物件不合法")
            conn.execute(
                "INSERT INTO ticket_items(ticket_no, garment, qty, remark) VALUES (?,?,?,?)",
                (ticket_no, garment, qty, (it.get("remark") or "").strip()),
            )
        conn.execute(
            """INSERT INTO process_events(ticket_no, from_status, to_status, operator, occurred_at, note)
               VALUES (?,?,?,?,?,?)""",
            (ticket_no, None, "已收", payload.get("operator") or "店员", now, "收衣开票"),
        )
        refresh_in_flight(conn)
    return get_ticket(ticket_no)


def advance(ticket_no: str, to_status: str, operator: str = "店员", note: str = "") -> dict:
    if is_day_sealed():
        raise HTTPException(409, "今日已结案，禁止改工序")
    to_status = (to_status or "").strip()
    with transaction() as conn:
        ensure_day_row(conn)
        row = _ticket_row(conn, ticket_no)
        if not row:
            raise HTTPException(404, "挂牌不存在")
        cur = row["status"]
        if cur in ("已取", "超期隔离", "丢失隔离"):
            raise HTTPException(409, f"当前态「{cur}」不可推进工序")
        allowed = FORWARD.get(cur, ())
        if to_status not in allowed:
            raise HTTPException(409, f"不可从「{cur}」到「{to_status}」")
        now = _now()
        conn.execute(
            "UPDATE hang_tickets SET status=? WHERE ticket_no=?",
            (to_status, ticket_no),
        )
        conn.execute(
            """INSERT INTO process_events(ticket_no, from_status, to_status, operator, occurred_at, note)
               VALUES (?,?,?,?,?,?)""",
            (ticket_no, cur, to_status, operator or "店员", now, note or ""),
        )
        refresh_in_flight(conn)
    return get_ticket(ticket_no)


def _normalize_selection(items: list[dict] | None, current: list[dict]) -> dict[int, int]:
    """把请求里的按件选择收敛成 {item_id: qty}。

    未给 items（或空）＝取走在册全部残留件。
    """
    by_id = {i["id"]: i for i in current}
    if not items:
        return {i["id"]: i["remaining"] for i in current if i["remaining"] > 0}
    wanted: dict[int, int] = {}
    for raw in items:
        try:
            item_id = int(raw.get("item_id"))
            qty = int(raw.get("qty"))
        except (TypeError, ValueError):
            raise HTTPException(400, "按件勾选需 item_id 与正整数 qty")
        if item_id not in by_id:
            raise HTTPException(400, f"衣物件 {item_id} 不属于该票")
        if qty <= 0:
            raise HTTPException(400, "取件数量须为正整数")
        wanted[item_id] = wanted.get(item_id, 0) + qty
    for item_id, qty in wanted.items():
        remaining = by_id[item_id]["remaining"]
        if qty > remaining:
            raise HTTPException(
                409,
                f"衣物件 {item_id} 仅剩 {remaining} 件，不能取走 {qty} 件",
            )
    return wanted


def pickup(
    ticket_no: str,
    pickup_code: str,
    operator: str = "店员",
    items: list[dict] | None = None,
    note: str = "",
) -> dict:
    """核销取衣：按件勾选、回执落账、票态变更、水位刷新同事务完成。"""
    with transaction() as conn:
        # 事务内复核日闸，避免与结案竞态。
        day = ensure_day_row(conn)
        gate = conn.execute("SELECT sealed FROM day_gates WHERE day=?", (day,)).fetchone()
        if gate and gate["sealed"]:
            raise HTTPException(409, "今日已结案，禁止取衣核销")

        row = _ticket_row(conn, ticket_no)
        if not row:
            raise HTTPException(404, "挂牌不存在")
        status = row["status"]
        if status not in PICKABLE:
            raise HTTPException(
                409, f"仅「待取」或「部分已取」可核销，当前「{status}」"
            )
        if (pickup_code or "").strip() != row["pickup_code"]:
            raise HTTPException(403, "取衣暗码不符")

        current = _items(conn, ticket_no)
        if not current:
            raise HTTPException(409, "该票无在册衣物件")
        if all(i["remaining"] == 0 for i in current):
            raise HTTPException(409, "在册衣物件已取齐，票态待核查")

        selection = _normalize_selection(items, current)
        if not selection:
            raise HTTPException(400, "至少勾选一件衣物")

        now = _now()
        seq = conn.execute(
            "SELECT COUNT(*) AS c FROM pickup_receipts WHERE ticket_no=?", (ticket_no,)
        ).fetchone()["c"]
        receipt_no = f"{ticket_no}-回执{seq + 1:02d}"
        cur = conn.execute(
            """INSERT INTO pickup_receipts
               (ticket_no, receipt_no, pickup_code, picked_at, operator, partial, note)
               VALUES (?,?,?,?,?,0,?)""",
            (ticket_no, receipt_no, row["pickup_code"], now, operator or "店员", note or ""),
        )
        receipt_id = cur.lastrowid

        for item_id, qty in selection.items():
            # 条件更新：件数上限在 SQL 层兜底，并发分取不会超取。
            upd = conn.execute(
                """UPDATE ticket_items
                      SET picked_qty = picked_qty + ?
                    WHERE id=? AND ticket_no=? AND picked_qty + ? <= qty""",
                (qty, item_id, ticket_no, qty),
            )
            if upd.rowcount != 1:
                raise HTTPException(
                    409, f"衣物件 {item_id} 可取件数不足，请刷新后重试"
                )
            conn.execute(
                """INSERT INTO pickup_lines
                   (receipt_id, ticket_no, item_id, qty, picked_at)
                   VALUES (?,?,?,?,?)""",
                (receipt_id, ticket_no, item_id, qty, now),
            )

        after = _items(conn, ticket_no)
        summary = _summarize(after)
        new_status = "已取" if summary["closed"] else PARTIAL

        conn.execute(
            "UPDATE pickup_receipts SET partial=? WHERE id=?",
            (0 if summary["closed"] else 1, receipt_id),
        )
        if summary["closed"]:
            conn.execute(
                "UPDATE hang_tickets SET status='已取', closed_at=? WHERE ticket_no=?",
                (now, ticket_no),
            )
            # 兼容旧版：整票取齐时落一行整票核销。
            conn.execute(
                """INSERT INTO pickups(ticket_no, pickup_code, picked_at, operator)
                   VALUES (?,?,?,?)
                   ON CONFLICT(ticket_no) DO UPDATE SET
                     picked_at=excluded.picked_at, operator=excluded.operator""",
                (ticket_no, row["pickup_code"], now, operator or "店员"),
            )
        else:
            conn.execute(
                "UPDATE hang_tickets SET status=? WHERE ticket_no=?",
                (PARTIAL, ticket_no),
            )

        detail = "、".join(
            f"件{item_id}×{qty}" for item_id, qty in selection.items()
        )
        event_note = f"取走{detail}；已取{summary['picked_qty']}/{summary['total_qty']}件"
        if note:
            event_note += f"；{note}"
        conn.execute(
            """INSERT INTO process_events(ticket_no, from_status, to_status, operator, occurred_at, note)
               VALUES (?,?,?,?,?,?)""",
            (ticket_no, status, new_status, operator or "店员", now, event_note),
        )
        refresh_in_flight(conn)

    data = get_ticket(ticket_no)
    data["回执"] = next(
        (r for r in data["receipts"] if r["receipt_no"] == receipt_no), None
    )
    return data


def quarantine(ticket_no: str, kind: str, reason: str, operator: str = "店员") -> dict:
    kind = (kind or "").strip()
    if kind not in ("超期隔离", "丢失隔离"):
        raise HTTPException(400, "隔离种类仅超期隔离或丢失隔离")
    with transaction() as conn:
        row = _ticket_row(conn, ticket_no)
        if not row:
            raise HTTPException(404, "挂牌不存在")
        if row["status"] in ("已取", "丢失隔离"):
            raise HTTPException(409, f"当前态「{row['status']}」不可隔离")
        if kind == "超期隔离" and row["status"] not in ("待取", "超期隔离"):
            raise HTTPException(409, "超期隔离仅从待取进入")
        now = _now()
        prev = row["status"]
        conn.execute(
            "UPDATE hang_tickets SET status=? WHERE ticket_no=?",
            (kind, ticket_no),
        )
        conn.execute(
            """INSERT INTO quarantines(ticket_no, kind, reason, created_at, resolved)
               VALUES (?,?,?,?,0)""",
            (ticket_no, kind, reason or kind, now),
        )
        conn.execute(
            """INSERT INTO process_events(ticket_no, from_status, to_status, operator, occurred_at, note)
               VALUES (?,?,?,?,?,?)""",
            (ticket_no, prev, kind, operator or "店员", now, reason or ""),
        )
        refresh_in_flight(conn)
    return get_ticket(ticket_no)


def audit_quantity() -> dict:
    """件数守恒重放：pickup_lines 汇总必须等于 ticket_items.picked_qty。"""
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT i.id AS item_id, i.ticket_no, i.qty, i.picked_qty,
                   COALESCE(SUM(l.qty), 0) AS replayed
              FROM ticket_items i
              LEFT JOIN pickup_lines l ON l.item_id = i.id
             GROUP BY i.id
             ORDER BY i.ticket_no, i.id
            """
        ).fetchall()
        mismatches = []
        for r in rows:
            d = dict(r)
            if d["replayed"] != d["picked_qty"] or not (0 <= d["picked_qty"] <= d["qty"]):
                mismatches.append(d)
        return {
            "核对件数": len(rows),
            "不符件数": len(mismatches),
            "守恒": not mismatches,
            "明细": mismatches,
        }
    finally:
        conn.close()

_pickup_impl = pickup


def pickup(ticket_no: str, pickup_code: str, operator: str = "店员", **kwargs):
    out = _pickup_impl(ticket_no, pickup_code, operator=operator, **kwargs) if 'pickup_code' in _pickup_impl.__code__.co_varnames else _pickup_impl(ticket_no, operator=operator, **kwargs)
    return partial_surface.after_pickup(ticket_no, out)

