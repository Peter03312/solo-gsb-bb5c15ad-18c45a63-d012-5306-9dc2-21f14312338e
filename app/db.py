"""SQLite 持久层：全局修订号、危险区不可变版本、生效时段、轨迹点。

- ``revisions`` 为全局严格递增修订号；每次危险区登记/同 ID 覆盖更新在单个
  ``BEGIN IMMEDIATE`` 事务内原子完成「分配修订号 + 写不可变版本 + 写时段」，
  因此并发更新不可能产生重号，查询也不可能读到半次提交。
- 每个修订号恰好为一个危险区生成一个不可变版本（``zone_versions`` +
  ``zone_version_windows``）；历史版本只追加、永不修改或删除。
- 写连接与读连接分离，读连接经 WAL 只能看到已提交快照；配合进程内全局锁，
  并发更新与查询互不串扰。
- 旧版本库中的现存 ``zones`` / ``zone_windows`` 在首次打开时一次性迁移为
  基线版本（按 zone_id 排序，每个现存区各占一个修订号），不推断迁移前历史。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .config import Settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS revisions (
    rev         INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS zone_versions (
    rev          INTEGER PRIMARY KEY,                 -- 修订号即版本号
    zone_id      TEXT NOT NULL,
    exterior     TEXT NOT NULL,                       -- GeoJSON-like [[x,y],...]（闭合）
    holes        TEXT NOT NULL,                       -- [[[x,y],...], ...]（各环闭合）
    created_at   TEXT NOT NULL,                       -- 该危险区首次登记时刻（沿用）
    FOREIGN KEY (rev) REFERENCES revisions(rev)
);
CREATE INDEX IF NOT EXISTS idx_zone_versions_zone_rev
    ON zone_versions(zone_id, rev);

CREATE TABLE IF NOT EXISTS zone_version_windows (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    rev          INTEGER NOT NULL REFERENCES zone_versions(rev),
    zone_id      TEXT NOT NULL,
    start_epoch  REAL NOT NULL,
    end_epoch    REAL NOT NULL,
    start_iso    TEXT NOT NULL,
    end_iso      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_zvw_rev ON zone_version_windows(rev);
CREATE INDEX IF NOT EXISTS idx_zvw_zone_rev ON zone_version_windows(zone_id, rev);

CREATE TABLE IF NOT EXISTS schema_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS points (
    trip_id    TEXT NOT NULL,
    point_id   TEXT NOT NULL,
    ts_epoch   REAL NOT NULL,
    x          REAL NOT NULL,
    y          REAL NOT NULL,
    PRIMARY KEY (trip_id, point_id)
);
-- 同车次同刻必须是同一个点：唯一索引防止 (trip, ts) 重复
CREATE UNIQUE INDEX IF NOT EXISTS idx_points_trip_ts
    ON points(trip_id, ts_epoch);
CREATE INDEX IF NOT EXISTS idx_points_trip ON points(trip_id, ts_epoch);
"""


def _connect(settings: Settings) -> sqlite3.Connection:
    conn = sqlite3.connect(
        settings.db_path,
        timeout=30,
        isolation_level=None,  # 显式事务
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


class Database:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        # 全局锁：写事务互斥；读操作同样持锁，保证查询与更新不交错
        self._lock = threading.RLock()
        self._w = _connect(settings)  # 写连接（BEGIN IMMEDIATE 串行化）
        self._r = _connect(settings)  # 读连接（WAL 已提交快照）
        self.init_schema()

    def init_schema(self) -> None:
        with self._lock:
            self._w.executescript(_SCHEMA)
            self._migrate_legacy_zones_locked()

    def close(self) -> None:
        with self._lock:
            self._w.close()
            self._r.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """在写连接上开启立即事务；持全局锁直到提交/回滚完成。"""
        conn = self._w
        with self._lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    # ----- 旧库迁移（基线版本） -------------------------------------------

    def _migrate_legacy_zones_locked(self) -> None:
        """把旧版 zones/zone_windows 现存区域一次性迁移为基线修订版本。

        每个现存区域按 zone_id 升序各占一个修订号；迁移结果原子提交并以
        schema_meta 标记，重复打开不会再次迁移，也不推断迁移前历史。
        """
        w = self._w
        done = w.execute(
            "SELECT value FROM schema_meta WHERE key = 'legacy_zones_migrated'"
        ).fetchone()
        if done is not None:
            return

        has_legacy = w.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'zones'"
        ).fetchone()

        w.execute("BEGIN IMMEDIATE")
        try:
            legacy_count = 0
            if has_legacy is not None:
                legacy_rows = list(w.execute("SELECT * FROM zones ORDER BY zone_id"))
                for row in legacy_rows:
                    zone_id = row["zone_id"]
                    cur = w.execute(
                        "INSERT INTO revisions(zone_id, created_at) VALUES (?, ?)",
                        (zone_id, row["created_at"]),
                    )
                    rev = int(cur.lastrowid)
                    w.execute(
                        "INSERT INTO zone_versions(rev, zone_id, exterior, holes, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (rev, zone_id, row["exterior"], row["holes"], row["created_at"]),
                    )
                    w.execute(
                        "INSERT INTO zone_version_windows"
                        "(rev, zone_id, start_epoch, end_epoch, start_iso, end_iso) "
                        "SELECT ?, zone_id, start_epoch, end_epoch, start_iso, end_iso "
                        "FROM zone_windows WHERE zone_id = ? ORDER BY start_epoch",
                        (rev, zone_id),
                    )
                    legacy_count += 1
            w.execute(
                "INSERT INTO schema_meta(key, value) VALUES ('legacy_zones_migrated', ?)",
                (str(legacy_count),),
            )
            w.execute("COMMIT")
        except Exception:
            w.execute("ROLLBACK")
            raise

    # ----- 修订号 ----------------------------------------------------------

    def current_revision(self) -> int:
        """当前最大修订号；尚无任何登记时为 0。"""
        with self._lock:
            row = self._r.execute("SELECT MAX(rev) AS m FROM revisions").fetchone()
            return int(row["m"] or 0)

    def alloc_revision(
        self, conn: sqlite3.Connection, zone_id: str, created_iso: str
    ) -> int:
        cur = conn.execute(
            "INSERT INTO revisions(zone_id, created_at) VALUES (?, ?)",
            (zone_id, created_iso),
        )
        return int(cur.lastrowid)

    def list_revision_rows(self, limit: int = 500) -> List[sqlite3.Row]:
        with self._lock:
            return list(
                self._r.execute(
                    "SELECT rev, zone_id, created_at FROM revisions "
                    "ORDER BY rev DESC LIMIT ?",
                    (limit,),
                )
            )

    def get_revision_row(self, rev: int) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._r.execute(
                "SELECT rev, zone_id, created_at FROM revisions WHERE rev = ?",
                (rev,),
            ).fetchone()

    # ----- 危险区不可变版本 ------------------------------------------------

    def insert_zone_version(
        self,
        conn: sqlite3.Connection,
        rev: int,
        zone_id: str,
        exterior: List[List[float]],
        holes: List[List[List[float]]],
        windows: List[Dict[str, Any]],
        created_iso: str,
    ) -> None:
        conn.execute(
            "INSERT INTO zone_versions(rev, zone_id, exterior, holes, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (rev, zone_id, json.dumps(exterior), json.dumps(holes), created_iso),
        )
        conn.executemany(
            "INSERT INTO zone_version_windows"
            "(rev, zone_id, start_epoch, end_epoch, start_iso, end_iso) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    rev,
                    zone_id,
                    win["start_epoch"],
                    win["end_epoch"],
                    win["start_iso"],
                    win["end_iso"],
                )
                for win in windows
            ],
        )

    def get_current_zone_row(self, zone_id: str) -> Optional[sqlite3.Row]:
        """某危险区的最新版本行；未登记返回 None。"""
        with self._lock:
            return self._r.execute(
                "SELECT * FROM zone_versions WHERE zone_id = ? ORDER BY rev DESC LIMIT 1",
                (zone_id,),
            ).fetchone()

    def list_current_zone_rows(self) -> List[sqlite3.Row]:
        """所有危险区各自的最新版本，按 zone_id 排序。"""
        with self._lock:
            return list(
                self._r.execute(
                    "SELECT zv.* FROM zone_versions zv "
                    "JOIN (SELECT zone_id, MAX(rev) AS max_rev FROM zone_versions "
                    "GROUP BY zone_id) m ON m.max_rev = zv.rev "
                    "ORDER BY zv.zone_id"
                )
            )

    def get_zone_version_at(self, zone_id: str, rev: int) -> Optional[sqlite3.Row]:
        """该区截至给定修订号的最后版本（rev <= 入参）；尚未登记返回 None。"""
        with self._lock:
            return self._r.execute(
                "SELECT * FROM zone_versions WHERE zone_id = ? AND rev <= ? "
                "ORDER BY rev DESC LIMIT 1",
                (zone_id, rev),
            ).fetchone()

    def list_zone_versions_at(self, rev: int) -> List[sqlite3.Row]:
        """截至给定修订号，所有已登记区各自的最后版本，按 zone_id 排序。

        修订号 > rev 才首次登记的区不出现（指定版本中尚未登记，不参与核算）。
        """
        with self._lock:
            return list(
                self._r.execute(
                    "SELECT zv.* FROM zone_versions zv "
                    "WHERE zv.rev = ("
                    "SELECT MAX(v.rev) FROM zone_versions v "
                    "WHERE v.zone_id = zv.zone_id AND v.rev <= ?"
                    ") ORDER BY zv.zone_id",
                    (rev,),
                )
            )

    def get_version_row_by_rev(self, rev: int) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._r.execute(
                "SELECT * FROM zone_versions WHERE rev = ?", (rev,)
            ).fetchone()

    def get_windows_for_rev(self, rev: int) -> List[Tuple[float, float]]:
        with self._lock:
            cur = self._r.execute(
                "SELECT start_epoch, end_epoch FROM zone_version_windows "
                "WHERE rev = ? ORDER BY start_epoch",
                (rev,),
            )
            return [(row["start_epoch"], row["end_epoch"]) for row in cur.fetchall()]

    def get_windows_iso_for_rev(self, rev: int) -> List[Dict[str, str]]:
        with self._lock:
            cur = self._r.execute(
                "SELECT start_iso, end_iso FROM zone_version_windows "
                "WHERE rev = ? ORDER BY start_epoch",
                (rev,),
            )
            return [
                {"start": row["start_iso"], "end": row["end_iso"]}
                for row in cur.fetchall()
            ]

    # ----- 轨迹点 ----------------------------------------------------------

    def get_point(self, trip_id: str, point_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._r.execute(
                "SELECT * FROM points WHERE trip_id = ? AND point_id = ?",
                (trip_id, point_id),
            ).fetchone()

    def find_point_at_ts(self, trip_id: str, ts_epoch: float) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._r.execute(
                "SELECT * FROM points WHERE trip_id = ? AND ts_epoch = ?",
                (trip_id, ts_epoch),
            ).fetchone()

    def list_trip_points(self, trip_id: str) -> List[sqlite3.Row]:
        with self._lock:
            return list(
                self._r.execute(
                    "SELECT * FROM points WHERE trip_id = ? ORDER BY ts_epoch, point_id",
                    (trip_id,),
                )
            )

    def insert_points(
        self, conn: sqlite3.Connection, rows: List[Tuple[str, str, float, float, float]]
    ) -> None:
        conn.executemany(
            "INSERT INTO points(trip_id, point_id, ts_epoch, x, y) VALUES (?,?,?,?,?)",
            rows,
        )
