"""巡检计划：赛前、赛中、赛后的时钟校验检查项。

赛前巡检决定靶位能否投入使用（放行闸门）；
赛中巡检核对采样纪律与隔离状态；
赛后巡检确认时间基准证据链完整，支撑成绩认证。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from .drift import DriftMonitor
from .models import fmt
from .rules import Rules


class Phase(Enum):
    PRE_MATCH = "pre_match"
    DURING_MATCH = "during_match"
    POST_MATCH = "post_match"


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    phase: Phase
    target_id: str
    passed: bool
    blocking: bool  # 未通过是否阻断（赛前）或须复核（赛后）
    detail: str


@dataclass
class InspectionReport:
    phase: Phase
    results: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if not r.passed]

    def targets_cleared(self) -> set[str]:
        """赛前巡检中全部检查通过的靶位。"""
        targets = {r.target_id for r in self.results}
        failed = {r.target_id for r in self.failures if r.blocking}
        return targets - failed


def pre_match(monitor: DriftMonitor, rules: Rules, target_ids: list[str],
              registered_firmware: dict[str, str],
              registered_sources: dict[str, str],
              now: datetime) -> InspectionReport:
    """赛前巡检：固件已登记、校准源有效、初始同步偏差合格。

    registered_firmware / registered_sources 均按靶位标识登记。
    """
    report = InspectionReport(phase=Phase.PRE_MATCH)
    for tid in target_ids:
        state = monitor.state(tid)
        report.results.append(CheckResult(
            "firmware-registered", Phase.PRE_MATCH, tid,
            passed=tid in registered_firmware, blocking=True,
            detail=f"固件版本 {registered_firmware[tid]}" if tid in registered_firmware
                   else "靶机固件未登记，禁止投入比赛",
        ))
        source_id = registered_sources.get(tid)
        report.results.append(CheckResult(
            "calibration-source", Phase.PRE_MATCH, tid,
            passed=source_id is not None, blocking=True,
            detail=f"校准源 {source_id}" if source_id else "该靶位无已登记校准源",
        ))
        initial = state.samples[-1] if state.samples else None
        ok = initial is not None and abs(initial.offset_ms) < rules.warn_offset_ms
        report.results.append(CheckResult(
            "initial-sync-offset", Phase.PRE_MATCH, tid,
            passed=ok, blocking=True,
            detail=(f"初始偏差 {initial.offset_ms:+.1f}ms（{fmt(initial.sampled_at)}）"
                    if initial else "无赛前同步采样"),
        ))
        report.results.append(CheckResult(
            "not-quarantined", Phase.PRE_MATCH, tid,
            passed=not state.quarantined, blocking=True,
            detail="靶位可用" if not state.quarantined else "靶位处于隔离状态",
        ))
    return report


def during_match(monitor: DriftMonitor, rules: Rules, target_ids: list[str],
                 now: datetime) -> InspectionReport:
    """赛中巡检：采样间隔合规、无未处置告警与隔离。"""
    report = InspectionReport(phase=Phase.DURING_MATCH)
    for tid in target_ids:
        state = monitor.state(tid)
        samples = state.samples
        if len(samples) >= 2:
            gaps = [
                (b.reference_time - a.reference_time).total_seconds()
                for a, b in zip(samples, samples[1:])
            ]
            worst = max(gaps)
            ok = worst <= rules.sync_interval_s
            detail = f"最大采样间隔 {worst:.0f}s（要求 ≤{rules.sync_interval_s:.0f}s）"
        else:
            ok = len(samples) == 1
            detail = "采样不足两次，暂无法评估间隔" if ok else "无同步采样"
        report.results.append(CheckResult(
            "sync-interval", Phase.DURING_MATCH, tid, passed=ok, blocking=False,
            detail=detail,
        ))
        report.results.append(CheckResult(
            "no-open-quarantine", Phase.DURING_MATCH, tid,
            passed=not state.quarantined, blocking=False,
            detail="无未解除隔离" if not state.quarantined
                   else f"隔离中：{state.quarantines[-1].reason}，待技术官员确认恢复",
        ))
    return report


def post_match(monitor: DriftMonitor, rules: Rules, target_ids: list[str],
               match_end: datetime) -> InspectionReport:
    """赛后巡检：末次采样够新、隔离均有技术官员确认的恢复记录。"""
    report = InspectionReport(phase=Phase.POST_MATCH)
    for tid in target_ids:
        state = monitor.state(tid)
        last = state.samples[-1] if state.samples else None
        age = (match_end - last.reference_time).total_seconds() if last else None
        ok = age is not None and age <= rules.final_sync_max_age_s
        report.results.append(CheckResult(
            "final-sync-fresh", Phase.POST_MATCH, tid, passed=ok, blocking=True,
            detail=(f"末次采样距比赛结束 {age:.0f}s（要求 ≤{rules.final_sync_max_age_s:.0f}s）"
                    if age is not None else "无任何同步采样"),
        ))
        unconfirmed = [q for q in state.quarantines if q.reinstated_by is None]
        report.results.append(CheckResult(
            "quarantines-adjudicated", Phase.POST_MATCH, tid,
            passed=not unconfirmed, blocking=True,
            detail=("全部隔离均有技术官员确认的处置" if not unconfirmed
                    else f"{len(unconfirmed)} 段隔离未经技术官员确认，相关发次不得直接认证"),
        ))
    return report
