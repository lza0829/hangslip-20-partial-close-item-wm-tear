from __future__ import annotations

from fastapi import APIRouter

from app.db import IN_FLIGHT_STATUSES, connect
from app.services.gate import today_str

router = APIRouter(tags=["总览"])


@router.get("/总览")
def dashboard():
    conn = connect()
    try:
        def cnt(status: str) -> int:
            return conn.execute(
                "SELECT COUNT(*) AS c FROM hang_tickets WHERE status=?", (status,)
            ).fetchone()["c"]

        day = today_str()
        gate = conn.execute("SELECT * FROM day_gates WHERE day=?", (day,)).fetchone()
        wm = conn.execute(
            "SELECT value FROM watermarks WHERE key='在途票'"
        ).fetchone()
        residual = conn.execute(
            "SELECT COALESCE(SUM(qty - picked_qty), 0) AS c FROM ticket_items"
        ).fetchone()["c"]
        # 水位口径（全局唯一）：在途票数必须能由票态重放，
        # 水位表只是缓存；两者不符即暴露，绝不拿缓存掩盖。
        placeholders = ",".join("?" for _ in IN_FLIGHT_STATUSES)
        in_flight = conn.execute(
            f"SELECT COUNT(*) AS c FROM hang_tickets WHERE status IN ({placeholders})",
            IN_FLIGHT_STATUSES,
        ).fetchone()["c"]
        stored_wm = wm["value"] if wm else 0
        return {
            "今日": day,
            "日闸": "已结案" if gate and gate["sealed"] else "开放",
            "在途水位": in_flight,
            "水位表缓存": stored_wm,
            "水位一致": stored_wm == in_flight,
            "已收": cnt("已收"),
            "水洗": cnt("水洗"),
            "烘干": cnt("烘干"),
            "熨烫": cnt("熨烫"),
            "待取": cnt("待取"),
            "部分已取": cnt("部分已取"),
            "已取": cnt("已取"),
            "超期隔离": cnt("超期隔离"),
            "丢失隔离": cnt("丢失隔离"),
            "残留件数": residual,
        }
    finally:
        conn.close()
