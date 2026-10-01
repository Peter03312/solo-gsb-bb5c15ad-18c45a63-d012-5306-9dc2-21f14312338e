"""修订号 / 不可变历史版本 / 按修订号复现穿越核算的测试。"""
import json
import sqlite3
import tempfile
import threading
from pathlib import Path

from app.config import get_settings
from app.db import Database
from app.service import AuditService, NotFoundError
from tests.conftest import DAY_WINDOWS, SQUARE, register_square


def _points_payload(points):
    return {"points": [{"point_id": pid, "ts": ts, "x": x, "y": y} for pid, ts, x, y in points]}


BASIC_POINTS = [
    ("p1", "2026-09-30T10:00:00Z", -50, 50),
    ("p2", "2026-09-30T10:00:40Z", 50, 50),
    ("p3", "2026-09-30T10:01:20Z", 150, 50),
]


# ---------------------------------------------------------------------------
# 登记返回修订号、全局递增
# ---------------------------------------------------------------------------

def test_register_returns_monotonic_global_revision(client):
    r1 = register_square(client, zone_id="z-a")
    assert r1.status_code == 201
    assert r1.json()["status"] == "created"
    assert r1.json()["revision"] == 1

    # 同 ID 覆盖更新 -> 新修订号
    r2 = register_square(
        client, zone_id="z-a",
        windows=[{"start": "2026-09-30T10:00:20Z", "end": "2026-09-30T10:00:50Z"}],
    )
    assert r2.json()["status"] == "updated"
    assert r2.json()["revision"] == 2

    # 另一个区 -> 全局继续递增
    r3 = register_square(client, zone_id="z-b")
    assert r3.json()["revision"] == 3

    r = client.get("/health")
    assert r.json()["current_revision"] == 3


def test_revisions_log_endpoint(client):
    register_square(client, zone_id="z-a")
    register_square(client, zone_id="z-a",
                    windows=[{"start": "2026-09-30T10:00:00Z", "end": "2026-09-30T11:00:00Z"}])
    register_square(client, zone_id="z-b")
    r = client.get("/api/v1/revisions")
    assert r.status_code == 200
    body = r.json()
    assert body["current_revision"] == 3
    revs = body["revisions"]
    assert [x["revision"] for x in revs] == [3, 2, 1]  # 倒序
    assert revs[0]["zone_id"] == "z-b"
    assert revs[1]["zone_id"] == "z-a"
    assert all(x["created_at"].endswith("Z") for x in revs)


# ---------------------------------------------------------------------------
# 不可变历史版本
# ---------------------------------------------------------------------------

def test_zone_history_is_immutable(client):
    register_square(client, zone_id="hist")
    register_square(
        client, zone_id="hist",
        windows=[{"start": "2026-09-30T10:00:20Z", "end": "2026-09-30T10:00:50Z"}],
    )

    v1 = client.get("/api/v1/zones/hist?revision=1").json()
    v2 = client.get("/api/v1/zones/hist?revision=2").json()
    latest = client.get("/api/v1/zones/hist").json()

    assert v1["revision"] == 1
    assert v1["as_of_revision"] == 1
    assert v1["windows"] == [
        {"start": "2026-09-30T00:00:00Z", "end": "2026-10-01T00:00:00Z"}
    ]
    assert v2["revision"] == 2
    assert v2["windows"] == [
        {"start": "2026-09-30T10:00:20Z", "end": "2026-09-30T10:00:50Z"}
    ]
    # 最新版本与 rev2 一致，但不带 as_of_revision
    assert latest["windows"] == v2["windows"]
    assert latest["revision"] == 2
    assert "as_of_revision" not in latest


def test_zone_geometry_history_reproduced(client):
    # rev1：0..100 大方框
    register_square(client, zone_id="geo")
    # rev2：40..60 小方框
    register_square(client, zone_id="geo",
                    exterior=[[40, 40], [60, 40], [60, 60], [40, 60]])
    client.post("/api/v1/trips/Tg/points", json=_points_payload(BASIC_POINTS))

    lat = client.get("/api/v1/trips/Tg/crossings?zone_id=geo").json()
    at1 = client.get("/api/v1/trips/Tg/crossings?zone_id=geo&revision=1").json()

    # 最新配置：直线 y=50 穿过小方框 x∈[40,60]，
    # 100m/40s → x=40 在 10:00:36，x=60 在 10:00:44，共 8 秒
    assert lat["zones"][0]["total_duration_sec"] == 8.0
    # 旧配置：大方框 40 秒
    assert at1["zones"][0]["total_duration_sec"] == 40.0


def test_zones_list_at_revision(client):
    register_square(client, zone_id="za")          # rev 1
    register_square(client, zone_id="zb")          # rev 2
    register_square(client, zone_id="za",
                    windows=[{"start": "2026-09-30T10:00:00Z",
                              "end": "2026-09-30T11:00:00Z"}])  # rev 3

    at2 = client.get("/api/v1/zones?revision=2").json()
    assert at2["revision"] == 2
    assert at2["current_revision"] == 3
    ids = {z["zone_id"]: z for z in at2["zones"]}
    assert set(ids) == {"za", "zb"}
    # za 取截至 rev2 的最后版本（即 rev1，全天窗口）
    assert ids["za"]["revision"] == 1
    assert ids["zb"]["revision"] == 2

    latest = client.get("/api/v1/zones").json()
    assert "revision" not in latest  # 未指定修订号：保持原响应语义
    assert {z["zone_id"] for z in latest["zones"]} == {"za", "zb"}


# ---------------------------------------------------------------------------
# 穿越核算按修订号复现
# ---------------------------------------------------------------------------

def test_crossings_pinned_revision_reproduces_old_windows(client):
    register_square(client, zone_id="win")  # rev1 全天生效
    client.post("/api/v1/trips/Tw/points", json=_points_payload(BASIC_POINTS))

    # rev2：仅 10:00:20~10:00:50 生效
    register_square(
        client, zone_id="win",
        windows=[{"start": "2026-09-30T10:00:20Z", "end": "2026-09-30T10:00:50Z"}],
    )

    latest = client.get("/api/v1/trips/Tw/crossings?zone_id=win").json()
    pinned2 = client.get("/api/v1/trips/Tw/crossings?zone_id=win&revision=2").json()
    pinned1 = client.get("/api/v1/trips/Tw/crossings?zone_id=win&revision=1").json()

    assert latest["zones"][0]["total_duration_sec"] == 30.0
    assert pinned2["zones"][0]["total_duration_sec"] == 30.0
    assert pinned1["zones"][0]["total_duration_sec"] == 40.0  # 旧配置全天窗口

    # 指定修订号时回显所用修订号；各区还标明自身版本号
    assert pinned1["revision"] == 1
    assert pinned1["current_revision"] == 2
    assert pinned1["zones"][0]["revision"] == 1
    # 未指定修订号时响应保持原样
    assert "revision" not in latest
    assert "current_revision" not in latest
    assert "revision" not in latest["zones"][0]


def test_zone_unregistered_at_revision_excluded(client):
    register_square(client, zone_id="late")  # rev1
    client.post("/api/v1/trips/Tx/points", json=_points_payload(BASIC_POINTS))
    register_square(client, zone_id="late",
                    windows=[{"start": "2026-09-30T10:00:20Z",
                              "end": "2026-09-30T10:00:50Z"}])  # rev2
    register_square(client, zone_id="newcomer")  # rev3

    # rev2 时 newcomer 尚未登记：不带过滤的查询中不参与核算
    r = client.get("/api/v1/trips/Tx/crossings?revision=2")
    zones = r.json()["zones"]
    assert [z["zone_id"] for z in zones] == ["late"]

    # 带 zone_id 过滤指定版本中未登记的区：不报错，空结果（不参与核算）
    r2 = client.get("/api/v1/trips/Tx/crossings?zone_id=newcomer&revision=2")
    assert r2.status_code == 200
    body = r2.json()
    assert body["zones"] == []
    assert body["revision"] == 2


def test_pinned_revision_error_cases(client):
    register_square(client, zone_id="e")
    client.post("/api/v1/trips/Te/points", json=_points_payload(BASIC_POINTS))

    # 超出当前范围 -> 明确 404
    r = client.get("/api/v1/trips/Te/crossings?revision=999")
    assert r.status_code == 404
    assert "999" in r.json()["detail"]

    # 非正 / 非整数 -> 明确报错（422 参数校验）
    assert client.get("/api/v1/trips/Te/crossings?revision=0").status_code == 422
    assert client.get("/api/v1/trips/Te/crossings?revision=-3").status_code == 422
    assert client.get("/api/v1/trips/Te/crossings?revision=abc").status_code == 422

    # 区域/列表端点同样校验
    assert client.get("/api/v1/zones?revision=999").status_code == 404
    assert client.get("/api/v1/zones/e?revision=999").status_code == 404
    assert client.get("/api/v1/zones/missing?revision=1").status_code == 404


def test_revision_beyond_current_on_empty_db_404(client):
    # 全新库尚无任何修订号
    r = client.get("/api/v1/zones?revision=1")
    assert r.status_code == 404
    r2 = client.get("/api/v1/trips/nope/crossings?revision=1")
    assert r2.status_code == 404


# ---------------------------------------------------------------------------
# 并发：不重号、不漏提交、查询不读半次提交
# ---------------------------------------------------------------------------

def test_concurrent_registrations_unique_revisions():
    from app.main import db as _db

    svc = AuditService(_db)
    n = 20
    results = []
    errors = []

    def worker(i):
        try:
            out = svc.register_zone(
                {
                    "zone_id": f"conc-{i}",
                    "exterior": SQUARE,
                    "holes": [],
                    "windows": DAY_WINDOWS,
                }
            )
            results.append(out["revision"])
        except Exception as exc:  # pragma: no cover - 仅用于暴露并发问题
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert sorted(results) == list(range(1, n + 1))
    assert svc.current_revision() == n
    # 所有版本均可读（无半次提交：版本与时段一起可见）
    for rev in range(1, n + 1):
        zrow = _db.get_version_row_by_rev(rev)
        assert zrow is not None
        assert _db.get_windows_for_rev(rev)


def test_concurrent_queries_during_updates_never_half_committed():
    from app.main import db as _db

    svc = AuditService(_db)
    svc.submit_points("trip-conc", {"points": [
        {"point_id": "c1", "ts": "2026-09-30T10:00:00Z", "x": -50, "y": 50},
        {"point_id": "c2", "ts": "2026-09-30T10:01:20Z", "x": 150, "y": 50},
    ]})

    stop = threading.Event()
    query_errors = []

    def reader():
        while not stop.is_set():
            try:
                cur = svc.current_revision()
                if cur >= 1:
                    # 任意已提交修订号都必须能取到自洽快照（版本行 + 时段齐备）
                    out = svc.query_crossings("trip-conc", revision=cur)
                    for z in out["zones"]:
                        _ = z["total_duration_sec"]
            except NotFoundError:
                pass  # 极端竞态下修订号被清理（本测试不会发生），容错
            except Exception as exc:  # pragma: no cover
                query_errors.append(exc)

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for t in readers:
        t.start()

    for i in range(15):
        svc.register_zone(
            {
                "zone_id": f"q-{i}",
                "exterior": SQUARE,
                "holes": [],
                "windows": DAY_WINDOWS,
            }
        )

    stop.set()
    for t in readers:
        t.join()
    assert query_errors == []
    assert svc.current_revision() == 15


# ---------------------------------------------------------------------------
# 旧库迁移：现存区域成为基线版本，不推断迁移前历史
# ---------------------------------------------------------------------------

def _build_legacy_db(path: Path, zones):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE zones (
            zone_id      TEXT PRIMARY KEY,
            exterior     TEXT NOT NULL,
            holes        TEXT NOT NULL,
            created_at   TEXT NOT NULL
        );
        CREATE TABLE zone_windows (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            zone_id      TEXT NOT NULL,
            start_epoch  REAL NOT NULL,
            end_epoch    REAL NOT NULL,
            start_iso    TEXT NOT NULL,
            end_iso      TEXT NOT NULL
        );
        """
    )
    for zid, exterior, windows in zones:
        conn.execute(
            "INSERT INTO zones(zone_id, exterior, holes, created_at) VALUES (?,?,?,?)",
            (zid, json.dumps(exterior), json.dumps([]), "2026-09-29T00:00:00Z"),
        )
        for s, e in windows:
            conn.execute(
                "INSERT INTO zone_windows(zone_id, start_epoch, end_epoch, start_iso, end_iso)"
                " VALUES (?,?,?,?,?)",
                (zid, 0.0, 0.0, s, e),
            )
    conn.commit()
    conn.close()


def test_legacy_zones_migrate_to_baseline():
    tmp = Path(tempfile.mkdtemp(prefix="dz-migrate-")) / "legacy.db"
    _build_legacy_db(
        tmp,
        [
            ("legacy-a", SQUARE, [("2026-09-30T00:00:00Z", "2026-10-01T00:00:00Z")]),
            ("legacy-b", SQUARE, [("2026-09-30T10:00:00Z", "2026-09-30T11:00:00Z")]),
        ],
    )

    class _MigSettings:
        db_path = tmp
        min_duration = get_settings().min_duration

    db1 = Database(_MigSettings())  # 首次打开即迁移
    svc = AuditService(db1)

    # 两个现存区按 zone_id 排序各占一个基线修订号
    assert svc.current_revision() == 2
    rows = db1.list_revision_rows(limit=10)
    assert [(r["rev"], r["zone_id"]) for r in rows] == [(2, "legacy-b"), (1, "legacy-a")]

    za = svc.get_zone("legacy-a")
    assert za is not None
    assert za["revision"] == 1
    assert za["created_at"] == "2026-09-29T00:00:00Z"

    # 迁移后再登记从修订号 3 继续；再次打开不重复迁移
    svc.register_zone(
        {"zone_id": "after", "exterior": SQUARE, "holes": [], "windows": DAY_WINDOWS}
    )
    db1.close()

    db2 = Database(_MigSettings())
    svc2 = AuditService(db2)
    assert svc2.current_revision() == 3
    detail = svc2.get_zone("legacy-a", revision=1)
    assert detail["windows"] == [
        {"start": "2026-09-30T00:00:00Z", "end": "2026-10-01T00:00:00Z"}
    ]
    # 不推断迁移前历史：修订号 0 非法、4 超范围
    try:
        svc2.query_crossings("whatever", revision=4)
        assert False, "应报 NotFoundError"
    except NotFoundError:
        pass
    db2.close()


def test_fresh_db_has_zero_revision_no_migration_needed():
    tmp = Path(tempfile.mkdtemp(prefix="dz-fresh-")) / "fresh.db"

    class _S:
        db_path = tmp
        min_duration = 1e-9

    db = Database(_S())
    assert db.current_revision() == 0
    assert db.list_revision_rows() == []
    # 新库中不应残留旧表
    has_old = db._r.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='zones'"
    ).fetchone()
    assert has_old is None
    db.close()
