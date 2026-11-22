"""多件票部分取件的联动与守恒测试。

口径（全局唯一）：票籍听件——
  取走部分 → 票「部分已取」、仍在途、水位不减；
  取齐最后一件 → 票「已取」、水位同步扣减；
  未取件始终可在残留侧核验；并发分取不得超取。
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import threading
from pathlib import Path

import pytest

TMP = Path(tempfile.mkdtemp(prefix="hangslip-test-"))
os.environ["HANGSLIP_TEST_DB"] = str(TMP / "test.db")


@pytest.fixture()
def client():
    # 每个测试一套干净的库与模块状态。
    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]
    import app.config as config

    config.DB_PATH = TMP / "test.db"
    # WAL 模式下旧库还会留下 -wal/-shm 旁路文件，必须一并清掉，
    # 否则上一个用例的残留行会让种子数据撞唯一约束。
    for suffix in ("", "-wal", "-shm"):
        side = TMP / f"test.db{suffix}"
        if side.exists():
            side.unlink()
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def open_ticket(client, ticket_no="挂-T1", items=None, code="暗码甲"):
    items = items or [{"garment": "衬衣", "qty": 3}, {"garment": "外套", "qty": 1}]
    r = client.post(
        "/api/挂牌",
        json={
            "ticket_no": ticket_no,
            "store_code": "店-河西",
            "pickup_code": code,
            "customer_name": "测试客",
            "items": items,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def to_ready(client, ticket_no):
    r = client.post(f"/api/挂牌/{ticket_no}/推进", json={"to_status": "待取"})
    assert r.status_code == 200, r.text
    return r.json()


def wm(client):
    return client.get("/api/总览").json()["在途水位"]


def residual(client):
    return client.get("/api/残留").json()


def test_full_pickup_closes_and_drops_watermark(client):
    open_ticket(client, "挂-FULL")
    to_ready(client, "挂-FULL")
    before = wm(client)
    r = client.post(
        "/api/挂牌/挂-FULL/取衣", json={"pickup_code": "暗码甲", "operator": "柜员"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "已取"
    assert body["remaining_qty"] == 0
    assert body["picked_qty"] == body["total_qty"] == 4
    assert wm(client) == before - 1
    assert [x for x in residual(client) if x["ticket_no"] == "挂-FULL"] == []


def test_partial_pickup_keeps_ticket_in_flight(client):
    open_ticket(client, "挂-PART")
    to_ready(client, "挂-PART")
    detail = client.get("/api/挂牌/挂-PART").json()
    shirt = next(i for i in detail["items"] if i["garment"] == "衬衣")
    before = wm(client)

    r = client.post(
        "/api/挂牌/挂-PART/取衣",
        json={
            "pickup_code": "暗码甲",
            "items": [{"item_id": shirt["id"], "qty": 2}],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "部分已取"
    assert body["picked_qty"] == 2
    assert body["remaining_qty"] == 2
    # 票还在途：水位不变。
    assert wm(client) == before
    # 待取页仍列出该票，且只剩未取件。
    ready = client.get("/api/待取").json()
    assert any(t["ticket_no"] == "挂-PART" for t in ready)
    # 残留侧可核验未取件。
    rows = [x for x in residual(client) if x["ticket_no"] == "挂-PART"]
    assert sum(x["remaining"] for x in rows) == 2
    # 尚未关票，不得确认已取。
    assert detail_after(client, "挂-PART")["status"] != "已取"


def detail_after(client, no):
    return client.get(f"/api/挂牌/{no}").json()


def test_pickup_beyond_remaining_rejected(client):
    open_ticket(client, "挂-OVER")
    to_ready(client, "挂-OVER")
    detail = client.get("/api/挂牌/挂-OVER").json()
    shirt = next(i for i in detail["items"] if i["garment"] == "衬衣")
    r = client.post(
        "/api/挂牌/挂-OVER/取衣",
        json={
            "pickup_code": "暗码甲",
            "items": [{"item_id": shirt["id"], "qty": 99}],
        },
    )
    assert r.status_code == 409
    # 被拒后件数与票态原封不动。
    after = client.get("/api/挂牌/挂-OVER").json()
    assert after["status"] == "待取"
    assert after["picked_qty"] == 0


def test_residual_visible_even_after_quarantine(client):
    # 丢失隔离可从任何未关票进入（含部分已取）；残留件仍可核验。
    open_ticket(client, "挂-ISO")
    to_ready(client, "挂-ISO")
    detail = client.get("/api/挂牌/挂-ISO").json()
    shirt = next(i for i in detail["items"] if i["garment"] == "衬衣")
    client.post(
        "/api/挂牌/挂-ISO/取衣",
        json={"pickup_code": "暗码甲", "items": [{"item_id": shirt["id"], "qty": 1}]},
    )
    r = client.post(
        "/api/挂牌/挂-ISO/隔离",
        json={"kind": "丢失隔离", "reason": "店内申报丢失"},
    )
    assert r.status_code == 200, r.text
    # 票态进隔离，但残留件仍可核验。
    rows = [x for x in residual(client) if x["ticket_no"] == "挂-ISO"]
    assert sum(x["remaining"] for x in rows) == 3
    # 隔离后不可再核销（避免重复取走）。
    r = client.post(
        "/api/挂牌/挂-ISO/取衣", json={"pickup_code": "暗码甲"}
    )
    assert r.status_code == 409


def test_overdue_quarantine_does_not_strand_partial_ticket(client):
    # 部分已取票仍可继续取件，故不走超期隔离这条会锁死残留的路径。
    open_ticket(client, "挂-LATE")
    to_ready(client, "挂-LATE")
    detail = client.get("/api/挂牌/挂-LATE").json()
    shirt = next(i for i in detail["items"] if i["garment"] == "衬衣")
    client.post(
        "/api/挂牌/挂-LATE/取衣",
        json={"pickup_code": "暗码甲", "items": [{"item_id": shirt["id"], "qty": 1}]},
    )
    r = client.post(
        "/api/挂牌/挂-LATE/隔离", json={"kind": "超期隔离", "reason": "逾承诺日"}
    )
    assert r.status_code == 409
    # 仍可把剩余件取齐关票。
    r = client.post(
        "/api/挂牌/挂-LATE/取衣", json={"pickup_code": "暗码甲"}
    )
    assert r.status_code == 200 and r.json()["status"] == "已取"


def test_quantity_conservation_after_mixed_pickups(client):
    open_ticket(client, "挂-CONS")
    to_ready(client, "挂-CONS")
    detail = client.get("/api/挂牌/挂-CONS").json()
    shirt = next(i for i in detail["items"] if i["garment"] == "衬衣")
    coat = next(i for i in detail["items"] if i["garment"] == "外套")

    client.post(
        "/api/挂牌/挂-CONS/取衣",
        json={"pickup_code": "暗码甲", "items": [{"item_id": shirt["id"], "qty": 1}]},
    )
    client.post(
        "/api/挂牌/挂-CONS/取衣",
        json={"pickup_code": "暗码甲", "items": [{"item_id": coat["id"], "qty": 1}]},
    )
    r = client.post(
        "/api/挂牌/挂-CONS/取衣",
        json={"pickup_code": "暗码甲", "items": [{"item_id": shirt["id"], "qty": 2}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "已取"

    audit = client.get("/api/核对/件数守恒").json()
    assert audit["守恒"] is True, audit
    # 三张回执：两张部分、一张关票。
    receipts = detail_after(client, "挂-CONS")["receipts"]
    assert len(receipts) == 3
    assert [x["partial"] for x in receipts] == [1, 1, 0]


def test_partial_ticket_cannot_advance_or_reclose(client):
    open_ticket(client, "挂-LOCK")
    to_ready(client, "挂-LOCK")
    detail = client.get("/api/挂牌/挂-LOCK").json()
    shirt = next(i for i in detail["items"] if i["garment"] == "衬衣")
    client.post(
        "/api/挂牌/挂-LOCK/取衣",
        json={"pickup_code": "暗码甲", "items": [{"item_id": shirt["id"], "qty": 1}]},
    )
    # 部分已取仍在途，不得借工序轨回退或推进。
    r = client.post("/api/挂牌/挂-LOCK/推进", json={"to_status": "熨烫"})
    assert r.status_code == 409
    # 取衣只会关票或落在部分已取，绝不会停在「待取」伪关票。
    after = detail_after(client, "挂-LOCK")
    assert after["status"] == "部分已取"
    assert after["closed_at"] is None


def test_code_checked_before_item_writes(client):
    open_ticket(client, "挂-CODE")
    to_ready(client, "挂-CODE")
    detail = client.get("/api/挂牌/挂-CODE").json()
    shirt = next(i for i in detail["items"] if i["garment"] == "衬衣")
    r = client.post(
        "/api/挂牌/挂-CODE/取衣",
        json={"pickup_code": "错码", "items": [{"item_id": shirt["id"], "qty": 1}]},
    )
    assert r.status_code == 403
    after = detail_after(client, "挂-CODE")
    assert after["picked_qty"] == 0
    assert after["status"] == "待取"


def test_concurrent_partial_pickups_never_overdraw(client):
    open_ticket(client, "挂-RACE", items=[{"garment": "衬衣", "qty": 5}])
    to_ready(client, "挂-RACE")
    detail = client.get("/api/挂牌/挂-RACE").json()
    item_id = detail["items"][0]["id"]

    results: list[int] = []
    lock = threading.Lock()

    def grab():
        r = client.post(
            "/api/挂牌/挂-RACE/取衣",
            json={"pickup_code": "暗码甲", "items": [{"item_id": item_id, "qty": 1}]},
        )
        with lock:
            results.append(r.status_code)

    threads = [threading.Thread(target=grab) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(200) == 5, results
    assert all(s in (200, 409) for s in results), results
    after = detail_after(client, "挂-RACE")
    assert after["picked_qty"] == 5
    assert after["remaining_qty"] == 0
    assert after["status"] == "已取"
    assert client.get("/api/核对/件数守恒").json()["守恒"] is True


def test_multi_line_pickup_rolls_back_as_one_when_overdraw(client):
    # 夹具「半截勾选件数不守恒」：一笔多件勾选里只要有一行超取，
    # 整笔回滚——回执、核销行、件位、票态、水位全部原地不动。
    open_ticket(client, "挂-ATOM", items=[{"garment": "衬衣", "qty": 2}, {"garment": "外套", "qty": 1}])
    to_ready(client, "挂-ATOM")
    detail = client.get("/api/挂牌/挂-ATOM").json()
    shirt = next(i for i in detail["items"] if i["garment"] == "衬衣")
    coat = next(i for i in detail["items"] if i["garment"] == "外套")
    before_wm = wm(client)

    r = client.post(
        "/api/挂牌/挂-ATOM/取衣",
        json={
            "pickup_code": "暗码甲",
            "items": [
                {"item_id": shirt["id"], "qty": 2},
                {"item_id": coat["id"], "qty": 5},  # 外套仅剩 1
            ],
        },
    )
    assert r.status_code == 409, r.text

    after = detail_after(client, "挂-ATOM")
    assert after["status"] == "待取"
    assert after["picked_qty"] == 0
    assert after["remaining_qty"] == 3
    assert after["receipts"] == []
    assert wm(client) == before_wm
    audit = client.get("/api/核对/件数守恒").json()
    assert audit["对账一致"] is True, audit


def test_cross_surface_witness_after_partial_then_close(client):
    # 取衣 API、勾选列表、待取墙、残留区、挂牌态（列表+详情）、水位总览互证。
    open_ticket(client, "挂-WIT", items=[{"garment": "衬衣", "qty": 2}, {"garment": "外套", "qty": 1}])
    to_ready(client, "挂-WIT")
    detail = client.get("/api/挂牌/挂-WIT").json()
    shirt = next(i for i in detail["items"] if i["garment"] == "衬衣")
    before_wm = wm(client)

    r = client.post(
        "/api/挂牌/挂-WIT/取衣",
        json={"pickup_code": "暗码甲", "items": [{"item_id": shirt["id"], "qty": 1}]},
    )
    assert r.status_code == 200

    # 1) 详情与列表页件数账一致。
    detail = detail_after(client, "挂-WIT")
    listed = next(t for t in client.get("/api/挂牌").json() if t["ticket_no"] == "挂-WIT")
    for view in (detail, listed):
        assert view["status"] == "部分已取"
        assert view["picked_qty"] == 1
        assert view["remaining_qty"] == 2
        assert view["closed_at"] is None

    # 2) 待取墙仍挂着该票，残留件口径与详情相同。
    ready = next(t for t in client.get("/api/待取").json() if t["ticket_no"] == "挂-WIT")
    assert sum(i["remaining"] for i in ready["remaining_items"]) == 2

    # 3) 残留区能核到两件未取，且票态不是关票态。
    residual_rows = [x for x in residual(client) if x["ticket_no"] == "挂-WIT"]
    assert sum(x["remaining"] for x in residual_rows) == 2
    assert all(x["ticket_status"] == "部分已取" for x in residual_rows)

    # 4) 部分取件水位绝不整减，且水位表与票态重放一致（不假绿）。
    dash = client.get("/api/总览").json()
    assert dash["在途水位"] == before_wm
    assert dash["水位一致"] is True
    assert dash["部分已取"] >= 1

    # 5) 取齐最后一件：关票、出途、水位减一、待取墙与残留区同时撤下。
    r = client.post("/api/挂牌/挂-WIT/取衣", json={"pickup_code": "暗码甲"})
    assert r.status_code == 200 and r.json()["status"] == "已取"
    assert wm(client) == before_wm - 1
    assert all(t["ticket_no"] != "挂-WIT" for t in client.get("/api/待取").json())
    assert [x for x in residual(client) if x["ticket_no"] == "挂-WIT"] == []
    closed = detail_after(client, "挂-WIT")
    assert closed["closed_at"] is not None and closed["remaining_qty"] == 0
    # 已关票不可再核销，守住件数上界。
    r = client.post("/api/挂牌/挂-WIT/取衣", json={"pickup_code": "暗码甲"})
    assert r.status_code == 409
    assert client.get("/api/核对/件数守恒").json()["对账一致"] is True


def test_poisoned_fake_partial_is_healed_from_item_bitmap(client):
    # 夹具「整票关仍有未勾件」：模拟旧假分取旁路制造的撕裂库——
    # 票态刷成已取、水位减一、写假绿缓存，但件位不动。重开库后必须
    # 按勾选位图收口，且收口后票能继续取件直到真关票。
    import app.db as dbmod

    open_ticket(client, "挂-HEAL1", items=[{"garment": "衬衣", "qty": 2}])
    to_ready(client, "挂-HEAL1")
    detail = client.get("/api/挂牌/挂-HEAL1").json()
    shirt = detail["items"][0]
    client.post(
        "/api/挂牌/挂-HEAL1/取衣",
        json={"pickup_code": "暗码甲", "items": [{"item_id": shirt["id"], "qty": 1}]},
    )
    # 一张一件未取的票，也被毒成已取。
    open_ticket(client, "挂-HEAL2", items=[{"garment": "大衣", "qty": 1}])
    to_ready(client, "挂-HEAL2")

    poisoned_at = "2026-01-01 09:00:00"
    conn = dbmod.connect()
    try:
        conn.execute(
            "UPDATE hang_tickets SET status='已取', closed_at=? WHERE ticket_no IN (?,?)",
            (poisoned_at, "挂-HEAL1", "挂-HEAL2"),
        )
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('partial_surface_ok', '1')"
        )
        # 假分取把水位无条件减一，与真实在途票数脱节。
        conn.execute("UPDATE watermarks SET value = MAX(value - 1, 0) WHERE key='在途票'")
        conn.commit()
    finally:
        conn.close()

    # 重新迁移即收口。
    dbmod.init_db()

    h1 = detail_after(client, "挂-HEAL1")
    assert h1["status"] == "部分已取"  # 有已取件、仍有残留
    assert h1["closed_at"] is None
    assert h1["picked_qty"] == 1 and h1["remaining_qty"] == 1
    h2 = detail_after(client, "挂-HEAL2")
    assert h2["status"] == "待取"        # 一件未取，回到待取
    assert h2["picked_qty"] == 0 and h2["remaining_qty"] == 1

    # 假绿缓存清掉；水位按票态重放复位；残留区可核；对账全绿。
    settings = client.get("/api/设置").json()
    assert not any(k.startswith("partial_surface") for k in settings)
    dash = client.get("/api/总览").json()
    assert dash["水位一致"] is True
    # 默认残留视图列出已开始取件的 HEAL1（1 件）；
    # 全量在册视图含尚未开始取件的 HEAL2，两件残留都可核。
    partial_residual = [x for x in residual(client) if x["ticket_no"] in ("挂-HEAL1", "挂-HEAL2")]
    assert sum(x["remaining"] for x in partial_residual) == 1
    full_residual = client.get("/api/残留?include_in_process=true").json()
    healed_residual = [x for x in full_residual if x["ticket_no"] in ("挂-HEAL1", "挂-HEAL2")]
    assert sum(x["remaining"] for x in healed_residual) == 2
    assert all(x["ticket_status"] != "已取" for x in healed_residual)
    audit = client.get("/api/核对/件数守恒").json()
    assert audit["对账一致"] is True, audit
    assert audit["票态不符"] == 0 and audit["关票残留"] == 0

    # 收口不是化妆：两张票都能凭暗码继续核销直至真关票。
    r = client.post("/api/挂牌/挂-HEAL1/取衣", json={"pickup_code": "暗码甲"})
    assert r.status_code == 200 and r.json()["status"] == "已取"
    r = client.post("/api/挂牌/挂-HEAL2/取衣", json={"pickup_code": "暗码甲"})
    assert r.status_code == 200 and r.json()["status"] == "已取"
    assert client.get("/api/核对/件数守恒").json()["对账一致"] is True
