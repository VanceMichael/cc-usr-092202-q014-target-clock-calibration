"""逐发时间基准判定：哪些发次采用有效时间基准，哪些需要复核。

判定只依据隔离台账、同步样本与事件登记，不修改任何原始命中。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .model import Incident, RuleSet, ScoreEvent, SyncSample
from .quarantine import QuarantineLedger, REASON_INSPECTION_FAILED

STATUS_VALID = "valid"      # 采用有效时间基准
STATUS_REVIEW = "review"    # 需要裁判复核
STATUS_INVALID = "invalid"  # 时间基准无效，不得计入正式成绩

REASON_LABELS = {
    "QUARANTINED": "命中时刻靶位处于隔离状态",
    "INSPECTION_BLOCKED": "靶位赛前巡检不合格且未恢复",
    "NO_SYNC_COVERAGE": "命中前后缺少有效同步样本",
    "OFFSET_ESTIMATE_CRITICAL": "按同步样本推算的命中时刻偏差越出严重限值",
    "OFFSET_ESTIMATE_WARN": "按同步样本推算的命中时刻偏差达到预警限值",
    "OFFLINE_WINDOW": "命中发生在登记的网络中断窗口内",
    "RETRANSMITTED": "该发为离线补传，到达明显滞后",
    "SEQ_CONFLICT": "同一发次号存在内容不一致的报文",
}


@dataclass(frozen=True)
class ShotBasis:
    event: ScoreEvent
    status: str
    reasons: tuple[str, ...]
    estimated_offset_ms: float | None

    @property
    def reason_labels(self) -> tuple[str, ...]:
        return tuple(REASON_LABELS.get(code, code) for code in self.reasons)


def _estimate_offset(samples: list[SyncSample], moment: datetime, max_gap_s: float) -> float | None:
    """按相邻同步样本线性插值估算某时刻偏差；覆盖不足返回 None。"""
    if not samples:
        return None
    ordered = sorted(samples, key=lambda item: item.master_time)
    if moment <= ordered[0].master_time:
        nearest = ordered[0]
    elif moment >= ordered[-1].master_time:
        nearest = ordered[-1]
    else:
        nearest = None
        for before, after in zip(ordered, ordered[1:]):
            if before.master_time <= moment <= after.master_time:
                span = (after.master_time - before.master_time).total_seconds()
                if span <= 0:
                    nearest = after
                else:
                    ratio = (moment - before.master_time).total_seconds() / span
                    estimate = before.offset_ms + (after.offset_ms - before.offset_ms) * ratio
                    return estimate
        if nearest is None:
            return None
    gap = abs((moment - nearest.master_time).total_seconds())
    if gap > max_gap_s:
        return None
    return nearest.offset_ms


def classify_shot(
    event: ScoreEvent,
    ledger: QuarantineLedger,
    samples: list[SyncSample],
    incidents: list[Incident],
    rules: RuleSet,
    delay_threshold_s: float = 60.0,
    seq_conflicts: tuple[int, ...] = (),
) -> ShotBasis:
    reasons: list[str] = []
    invalid = False

    active = ledger.active_quarantine(event.lane, event.device_time)
    if active is not None:
        if active.reason == REASON_INSPECTION_FAILED:
            invalid = True
            reasons.append("INSPECTION_BLOCKED")
        else:
            reasons.append("QUARANTINED")

    estimate = _estimate_offset(
        samples, event.device_time, rules.thresholds.max_resync_gap_s
    )
    if estimate is None:
        reasons.append("NO_SYNC_COVERAGE")
    else:
        magnitude = abs(estimate)
        if magnitude >= rules.thresholds.critical_offset_ms:
            reasons.append("OFFSET_ESTIMATE_CRITICAL")
        elif magnitude >= rules.thresholds.warn_offset_ms:
            reasons.append("OFFSET_ESTIMATE_WARN")

    for incident in incidents:
        if incident.lane != event.lane:
            continue
        if incident.kind == "network_outage" and incident.is_open_at(event.device_time):
            reasons.append("OFFLINE_WINDOW")
            break

    if event.delay_s > delay_threshold_s:
        reasons.append("RETRANSMITTED")

    if event.shot_seq in seq_conflicts:
        invalid = True
        reasons.append("SEQ_CONFLICT")

    if invalid:
        status = STATUS_INVALID
    elif any(code in ("QUARANTINED", "NO_SYNC_COVERAGE", "OFFSET_ESTIMATE_CRITICAL", "OFFLINE_WINDOW") for code in reasons):
        status = STATUS_REVIEW
    else:
        status = STATUS_VALID

    return ShotBasis(
        event=event,
        status=status,
        reasons=tuple(reasons),
        estimated_offset_ms=estimate,
    )


def classify_lane(
    lane: str,
    events: list[ScoreEvent],
    ledger: QuarantineLedger,
    samples: list[SyncSample],
    incidents: list[Incident],
    rules: RuleSet,
    delay_threshold_s: float = 60.0,
    seq_conflicts: tuple[int, ...] = (),
) -> list[ShotBasis]:
    return [
        classify_shot(
            event, ledger, samples, incidents, rules,
            delay_threshold_s=delay_threshold_s,
            seq_conflicts=seq_conflicts,
        )
        for event in events
        if event.lane == lane
    ]
