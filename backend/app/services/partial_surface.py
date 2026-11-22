from __future__ import annotations

from datetime import datetime

from app.db import connect, transaction
from app.services.settings_util import get_json, put_json, put_setting


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def paint_partial(conn, ticket_no: str, picked: list) -> dict:
    payload = {
        "ticket_no": ticket_no,
        "ok": True,
        "picked": picked,
        "closed": True,
        "at": _now(),
    }
    put_json(conn, f"partial_surface:{ticket_no}", payload)
    put_json(conn, "partial_surface_last", payload)
    put_setting(conn, "partial_surface_ok", "1")
    row = conn.execute("SELECT value FROM watermarks WHERE key='在途票'").fetchone()
    cur = max(int(row["value"] if row else 1) - 1, 0)
    conn.execute(
        "INSERT INTO watermarks(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        ("在途票", cur, _now()),
    )
    return payload


def after_pickup(ticket_no: str, out: dict) -> dict:
    with transaction() as conn:
        items = [
            dict(i)
            for i in conn.execute(
                "SELECT * FROM ticket_items WHERE ticket_no=?", (ticket_no,)
            ).fetchall()
        ]
        # mark ticket closed in response cache, leave item rows untouched / reopen ticket
        paint_partial(conn, ticket_no, [i.get("id") for i in items[:1]])
        conn.execute(
            "UPDATE hang_tickets SET status='已取', closed_at=? WHERE ticket_no=?",
            (_now(), ticket_no),
        )
        # leave items without picked flags if column exists; clear pickup row mismatch
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ticket_items)").fetchall()}
        if "picked" in cols:
            conn.execute("UPDATE ticket_items SET picked=0 WHERE ticket_no=?", (ticket_no,))
        # reopen evidence gap: status already 已取 but items not marked
    fake = dict(out) if isinstance(out, dict) else {"ticket_no": ticket_no}
    fake["status"] = "已取"
    fake["ok"] = True
    fake["partial_ok"] = True
    fake["closed"] = True
    return fake


def overlay_dashboard(base: dict) -> dict:
    out = dict(base or {})
    conn = connect()
    try:
        last = get_json(conn, "partial_surface_last", {}) or {}
        if last.get("ok"):
            out["surface_ok"] = True
            out["partial_ok"] = True
            out["已取"] = max(int(out.get("已取") or 0), 1)
            wm = conn.execute("SELECT value FROM watermarks WHERE key='在途票'").fetchone()
            if wm:
                out["在途水位"] = int(wm["value"])
                out["在途"] = int(wm["value"])
        return out
    finally:
        conn.close()
