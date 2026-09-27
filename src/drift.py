"""同步样本分析：偏差分级、漂移速率、同步缺口与越界时刻。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .model import Incident, RuleSet, SyncSample

LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_CRITICAL = "critical"
LEVEL_INFO = "info"


@dataclass(frozen=True)
class DriftPoint:
    """单个同步样本的评估结果。"""

    lane: str
    seq: int
    at: datetime
    offset_ms: float
    offset_level: str
    drift_ppm: float | None
    drift_level: str


@dataclass(frozen=True)
class Alert:
    """实时漂移告警（状态迁移时产生，避免重复刷屏）。"""

    lane: str
    level: str  # warn / critical / info
    code: str
    at: datetime
    message: str
    value: float | None = None


def _offset_level(offset_ms: float, rules: RuleSet) -> str:
    magnitude = abs(offset_ms)
    if magnitude >= rules.thresholds.critical_offset_ms:
        return LEVEL_CRITICAL
    if magnitude >= rules.thresholds.warn_offset_ms:
        return LEVEL_WARN
    return LEVEL_OK


def _drift_level(ppm: float, rules: RuleSet) -> str:
    magnitude = abs(ppm)
    if magnitude >= rules.thresholds.critical_drift_ppm:
        return LEVEL_CRITICAL
    if magnitude >= rules.thresholds.warn_drift_ppm:
        return LEVEL_WARN
    return LEVEL_OK


def assess_lane(lane: str, samples: list[SyncSample], rules: RuleSet,
                incidents: list[Incident] | None = None):
    """返回 (逐点评估, 告警列表, 首次严重越界时刻)。

    同步缺口告警只在样本间隔与登记的网络中断/人工事件重叠时产生，
    正常的周期性轮询间隔不算中断。
    """
    incidents = incidents or []
    ordered = sorted(samples, key=lambda item: (item.master_time, item.seq))
    points: list[DriftPoint] = []
    alerts: list[Alert] = []
    first_critical_at: datetime | None = None
    prev_level = LEVEL_OK

    for index, sample_item in enumerate(ordered):
        level = _offset_level(sample_item.offset_ms, rules)
        ppm: float | None = None
        dlevel = LEVEL_OK

        if index > 0:
            previous = ordered[index - 1]
            dt_s = (sample_item.master_time - previous.master_time).total_seconds()
            if dt_s > 0:
                ppm = ((sample_item.offset_ms - previous.offset_ms) / 1000.0) / dt_s * 1e6
                dlevel = _drift_level(ppm, rules)
                if dlevel == LEVEL_CRITICAL:
                    alerts.append(Alert(
                        lane, LEVEL_CRITICAL, "DRIFT_CRITICAL", sample_item.master_time,
                        f"漂移速率 {ppm:+.0f} ppm 达到严重限值", ppm,
                    ))
                elif dlevel == LEVEL_WARN:
                    alerts.append(Alert(
                        lane, LEVEL_WARN, "DRIFT_WARN", sample_item.master_time,
                        f"漂移速率 {ppm:+.0f} ppm 达到预警限值", ppm,
                    ))
                if dt_s > rules.thresholds.max_resync_gap_s:
                    gap_start = previous.master_time
                    gap_end = sample_item.master_time
                    covered = any(
                        incident.lane == lane
                        and incident.start <= gap_end
                        and (incident.end is None or incident.end >= gap_start)
                        for incident in incidents
                    )
                    if covered:
                        alerts.append(Alert(
                            lane, LEVEL_WARN, "SYNC_GAP", sample_item.master_time,
                            f"登记事件期间同步中断 {dt_s / 60:.1f} 分钟，超过 "
                            f"{rules.thresholds.max_resync_gap_s / 60:.1f} 分钟", dt_s,
                        ))

        if sample_item.source not in rules.calibration_sources:
            alerts.append(Alert(
                lane, LEVEL_CRITICAL, "SOURCE_UNREGISTERED", sample_item.master_time,
                f"校准源 {sample_item.source} 未登记", None,
            ))

        if level == LEVEL_CRITICAL and prev_level != LEVEL_CRITICAL:
            alerts.append(Alert(
                lane, LEVEL_CRITICAL, "OFFSET_CRITICAL", sample_item.master_time,
                f"同步偏差 {sample_item.offset_ms:+.0f} ms 越出严重限值 "
                f"{rules.thresholds.critical_offset_ms:.0f} ms", sample_item.offset_ms,
            ))
            if first_critical_at is None:
                first_critical_at = sample_item.master_time
        elif level == LEVEL_WARN and prev_level == LEVEL_OK:
            alerts.append(Alert(
                lane, LEVEL_WARN, "OFFSET_WARN", sample_item.master_time,
                f"同步偏差 {sample_item.offset_ms:+.0f} ms 达到预警限值",
                sample_item.offset_ms,
            ))
        elif level == LEVEL_OK and prev_level != LEVEL_OK:
            alerts.append(Alert(
                lane, LEVEL_INFO, "OFFSET_RECOVERED", sample_item.master_time,
                f"同步偏差恢复至 {sample_item.offset_ms:+.0f} ms", sample_item.offset_ms,
            ))
        prev_level = level

        points.append(DriftPoint(
            lane=lane,
            seq=sample_item.seq,
            at=sample_item.master_time,
            offset_ms=sample_item.offset_ms,
            offset_level=level,
            drift_ppm=ppm,
            drift_level=dlevel,
        ))

    return points, alerts, first_critical_at
