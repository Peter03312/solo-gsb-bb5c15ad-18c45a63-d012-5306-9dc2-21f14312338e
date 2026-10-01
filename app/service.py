"""业务服务：危险区登记（带全局修订号/不可变版本）、轨迹点批量提交、穿越审计。"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from shapely.geometry import Polygon

from .db import Database
from .geometry import GeometryError, build_polygon, merge_intervals, segment_inside_params
from .timeutils import TimeError, clip_intervals_halfopen, merge_windows, parse_utc, validate_window


class ConflictError(Exception):
    """内容冲突（HTTP 409）。"""


class ValidationError(Exception):
    """语义校验失败（HTTP 400）。"""


class NotFoundError(Exception):
    """目标不存在/超出当前范围（HTTP 404）。"""


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class AuditService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.min_duration = db.settings.min_duration

    # ------------------------------------------------------------------
    # 修订号
    # ------------------------------------------------------------------

    def current_revision(self) -> int:
        return self.db.current_revision()

    def list_revisions(self, limit: int = 500) -> Dict[str, Any]:
        return {
            "current_revision": self.db.current_revision(),
            "revisions": [
                {"revision": row["rev"], "zone_id": row["zone_id"], "created_at": row["created_at"]}
                for row in self.db.list_revision_rows(limit=limit)
            ],
        }

    def _validate_revision(self, revision: Optional[int]) -> Optional[int]:
        """校验穿越/查询指定的修订号：必须为 >=1 且不超过当前修订号。"""
        if revision is None:
            return None
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise ValidationError("revision 必须是不小于 1 的整数")
        current = self.db.current_revision()
        if revision > current:
            raise NotFoundError(
                f"修订号 {revision} 超出当前范围（当前最大修订号为 {current}）"
            )
        return revision

    # ------------------------------------------------------------------
    # 危险区登记
    # ------------------------------------------------------------------

    def register_zone(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        zone_id = payload.get("zone_id")
        if not isinstance(zone_id, str) or not zone_id.strip():
            raise ValidationError("zone_id 必须是非空字符串")
        zone_id = zone_id.strip()

        exterior = payload.get("exterior")
        holes = payload.get("holes") or []
        if not isinstance(holes, list):
            raise ValidationError("holes 必须是环列表")
        try:
            polygon = build_polygon(exterior, holes)
        except GeometryError as exc:
            raise ValidationError(str(exc)) from exc

        raw_windows = payload.get("windows")
        if not isinstance(raw_windows, list) or not raw_windows:
            raise ValidationError("windows 必须是非空左闭右开时段列表 [{start, end}]")
        windows: List[Dict[str, Any]] = []
        for idx, w in enumerate(raw_windows):
            if not isinstance(w, dict):
                raise ValidationError(f"windows[{idx}] 必须是对象")
            try:
                start = parse_utc(w.get("start"), field=f"windows[{idx}].start")
                end = parse_utc(w.get("end"), field=f"windows[{idx}].end")
                validate_window(start, end)
            except TimeError as exc:
                raise ValidationError(str(exc)) from exc
            windows.append(
                {
                    "start_epoch": start.timestamp(),
                    "end_epoch": end.timestamp(),
                    "start_iso": _iso_z(start),
                    "end_iso": _iso_z(end),
                }
            )

        now_iso = _iso_z(datetime.now(timezone.utc))
        with self.db.transaction() as conn:
            # 登记/覆盖更新原子地：分配全局递增修订号 + 写不可变版本
            existing = self.db.get_current_zone_row(zone_id)
            created = existing is None
            created_at = now_iso if created else existing["created_at"]
            rev = self.db.alloc_revision(conn, zone_id, now_iso)
            self.db.insert_zone_version(
                conn,
                rev,
                zone_id,
                [list(c) for c in polygon.exterior.coords],
                [[list(c) for c in ring.coords] for ring in polygon.interiors],
                windows,
                created_at,
            )

        return {
            "zone_id": zone_id,
            "status": "created" if created else "updated",
            "revision": rev,
            "num_holes": len(polygon.interiors),
            "num_windows": len(windows),
        }

    def _zone_detail_from_row(
        self, row: Any, *, revision: Optional[int] = None
    ) -> Dict[str, Any]:
        detail = {
            "zone_id": row["zone_id"],
            "exterior": json.loads(row["exterior"]),
            "holes": json.loads(row["holes"]),
            "windows": self.db.get_windows_iso_for_rev(int(row["rev"])),
            "created_at": row["created_at"],
            "revision": int(row["rev"]),
        }
        if revision is not None:
            detail["as_of_revision"] = revision
        return detail

    def get_zone(
        self, zone_id: str, revision: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        revision = self._validate_revision(revision)
        if revision is None:
            row = self.db.get_current_zone_row(zone_id)
        else:
            row = self.db.get_zone_version_at(zone_id, revision)
        if row is None:
            return None
        return self._zone_detail_from_row(row, revision=revision)

    def list_zones(self, revision: Optional[int] = None) -> Dict[str, Any]:
        revision = self._validate_revision(revision)
        if revision is None:
            rows = self.db.list_current_zone_rows()
            result = [self._zone_detail_from_row(row) for row in rows]
            return {"zones": result}
        rows = self.db.list_zone_versions_at(revision)
        return {
            "revision": revision,
            "current_revision": self.db.current_revision(),
            "zones": [
                self._zone_detail_from_row(row, revision=revision) for row in rows
            ],
        }

    # ------------------------------------------------------------------
    # 轨迹点批量提交（幂等 / 冲突）
    # ------------------------------------------------------------------

    def submit_points(self, trip_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(trip_id, str) or not trip_id.strip():
            raise ValidationError("trip_id 必须是非空字符串")
        trip_id = trip_id.strip()

        points = payload.get("points")
        if not isinstance(points, list) or not points:
            raise ValidationError("points 必须是非空轨迹点列表")

        # ---- 批内解析与校验 ----
        parsed: List[Tuple[str, float, float, float, str]] = []
        # point_id -> (ts,x,y,ts_iso)；ts -> point_id，用于批内一致性检查
        seen_ids: Dict[str, Tuple[float, float, float, str]] = {}
        seen_ts: Dict[float, str] = {}

        for idx, item in enumerate(points):
            if not isinstance(item, dict):
                raise ValidationError(f"points[{idx}] 必须是对象")
            point_id = item.get("point_id")
            if not isinstance(point_id, str) or not point_id.strip():
                raise ValidationError(f"points[{idx}].point_id 必须是非空字符串")
            point_id = point_id.strip()

            try:
                dt = parse_utc(item.get("ts"), field=f"points[{idx}].ts")
            except TimeError as exc:
                raise ValidationError(str(exc)) from exc

            x_raw, y_raw = item.get("x"), item.get("y")
            if isinstance(x_raw, bool) or isinstance(y_raw, bool):
                raise ValidationError(f"points[{idx}] 坐标必须是数值")
            try:
                x, y = float(x_raw), float(y_raw)
            except (TypeError, ValueError):
                raise ValidationError(
                    f"points[{idx}] 坐标必须是数值: x={x_raw!r}, y={y_raw!r}"
                )

            if not (math.isfinite(x) and math.isfinite(y)):
                raise ValidationError(
                    f"points[{idx}] 坐标必须有限，禁止 NaN/Infinity: x={x_raw!r}, y={y_raw!r}"
                )

            ts_epoch = dt.timestamp()
            sig = (ts_epoch, x, y)

            # 同 point_id 批内重复：内容一致幂等，不一致冲突
            if point_id in seen_ids:
                if seen_ids[point_id][:3] != sig:
                    raise ConflictError(
                        f"批内冲突: point_id={point_id!r} 出现了不同内容（时刻或坐标不一致）"
                    )
                continue

            # 同车次同刻不同点
            if ts_epoch in seen_ts:
                raise ConflictError(
                    f"批内冲突: 时刻 {_iso_z(dt)} 同时关联了 point_id="
                    f"{seen_ts[ts_epoch]!r} 与 {point_id!r}"
                )

            seen_ids[point_id] = (ts_epoch, x, y, _iso_z(dt))
            seen_ts[ts_epoch] = point_id
            parsed.append((point_id, ts_epoch, x, y, _iso_z(dt)))

        # ---- 与已存数据比对（只读阶段，先全部收集错误） ----
        stored_ids: Dict[str, Tuple[float, float, float]] = {}
        for point_id, ts_epoch, x, y, _ in parsed:
            row = self.db.get_point(trip_id, point_id)
            if row is not None:
                stored = (row["ts_epoch"], row["x"], row["y"])
                incoming = (ts_epoch, x, y)
                if stored != incoming:
                    raise ConflictError(
                        f"point_id={point_id!r} 已存在且内容不同："
                        f"已存 (ts_epoch={stored[0]}, x={stored[1]}, y={stored[2]})，"
                        f"新传 (ts_epoch={ts_epoch}, x={x}, y={y})"
                    )
                stored_ids[point_id] = incoming
                continue
            # 新点：检查是否与已有同刻点撞车
            other = self.db.find_point_at_ts(trip_id, ts_epoch)
            if other is not None:
                raise ConflictError(
                    f"时刻冲突: point_id={point_id!r} 的时刻与已存 point_id="
                    f"{other['point_id']!r} 相同（同车次同刻只能有一个点）"
                )

        # ---- 单事务写入新点 ----
        new_rows = [
            (trip_id, point_id, ts_epoch, x, y)
            for point_id, ts_epoch, x, y, _ in parsed
            if point_id not in stored_ids
        ]
        with self.db.transaction() as conn:
            self.db.insert_points(conn, new_rows)

        return {
            "trip_id": trip_id,
            "received": len(points),
            "accepted_new": len(new_rows),
            "idempotent_retransmit": len(points) - len(new_rows),
        }

    # ------------------------------------------------------------------
    # 穿越审计
    # ------------------------------------------------------------------

    def _crossing_for_zone_version(
        self,
        points: List[Any],
        zrow: Any,
        lo: Optional[float],
        hi: Optional[float],
        include_revision: bool = False,
    ) -> Dict[str, Any]:
        polygon = Polygon(
            json.loads(zrow["exterior"]),
            json.loads(zrow["holes"]),
        )
        windows = merge_windows(self.db.get_windows_for_rev(int(zrow["rev"])))

        # 覆盖参数（时间）区间：遍历相邻点，乱序已在 SQL 层按时刻排序重建
        cover: List[Tuple[float, float]] = []
        for a, b in zip(points, points[1:]):
            t0, t1 = a["ts_epoch"], b["ts_epoch"]
            if t1 <= t0:
                # 同刻点不应出现（唯一约束），稳妥跳过
                continue
            params = segment_inside_params(
                polygon, (a["x"], a["y"]), (b["x"], b["y"])
            )
            for pa, pb in params:
                # 参数 0/1 直接取端点时刻，保证相邻段在公共采样点精确相接
                s = t0 if pa == 0.0 else t0 + pa * (t1 - t0)
                e = t1 if pb == 1.0 else t0 + pb * (t1 - t0)
                if e - s > self.min_duration:
                    cover.append((s, e))

        # 时间域合并留 1e-6 秒容差，吸收不同时刻解析路径的浮点误差
        cover = merge_intervals(cover, eps=1e-6)
        intervals = clip_intervals_halfopen(cover, windows)
        if lo is not None or hi is not None:
            bounds = [(-float("inf") if lo is None else lo, float("inf") if hi is None else hi)]
            intervals = clip_intervals_halfopen(intervals, bounds)
        intervals = merge_intervals(intervals, eps=1e-6)
        intervals = [(s, e) for s, e in intervals if e - s > self.min_duration]

        segments = [
            {
                "start": _iso_z(datetime.fromtimestamp(s, timezone.utc)),
                "end": _iso_z(datetime.fromtimestamp(e, timezone.utc)),
                "duration_sec": round(e - s, 6),
            }
            for s, e in intervals
        ]
        total = round(sum(e - s for s, e in intervals), 6)

        entry: Dict[str, Any] = {
            "zone_id": zrow["zone_id"],
            "crossed": bool(intervals),
            "total_duration_sec": total,
            "segments": segments,
        }
        if include_revision:
            # 仅在按修订号复现时附加该区实际使用的版本号；
            # 最新配置查询严格保持原有响应字段。
            entry["revision"] = int(zrow["rev"])
        return entry

    def query_crossings(
        self,
        trip_id: str,
        zone_id: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        revision = self._validate_revision(revision)

        try:
            lo = parse_utc(since, field="since").timestamp() if since else None
            hi = parse_utc(until, field="until").timestamp() if until else None
        except TimeError as exc:
            raise ValidationError(str(exc)) from exc
        if lo is not None and hi is not None and lo >= hi:
            raise ValidationError("since 必须早于 until（查询范围为半开 [since, until)）")

        points = self.db.list_trip_points(trip_id)

        # 选定本次核算所用的区域配置
        if revision is None:
            if zone_id is not None:
                current = self.db.get_current_zone_row(zone_id)
                if current is None:
                    raise ValidationError(f"危险区不存在: {zone_id}")
                zone_rows = [current]
            else:
                zone_rows = self.db.list_current_zone_rows()
        else:
            # 指定修订号：取各区截至该号的最后版本；当时尚未登记的区不参与核算
            if zone_id is not None:
                row = self.db.get_zone_version_at(zone_id, revision)
                zone_rows = [] if row is None else [row]
            else:
                zone_rows = self.db.list_zone_versions_at(revision)

        if len(points) < 2:
            result: Dict[str, Any] = {
                "trip_id": trip_id,
                "point_count": len(points),
                "zones": [],
            }
        else:
            result = {
                "trip_id": trip_id,
                "point_count": len(points),
                "zones": [
                    self._crossing_for_zone_version(
                        points, zrow, lo, hi, include_revision=revision is not None
                    )
                    for zrow in zone_rows
                ],
            }

        # 指定修订号时回显实际所用配置版本；未指定时保持原有响应语义
        if revision is not None:
            result["revision"] = revision
            result["current_revision"] = self.db.current_revision()
        return result
