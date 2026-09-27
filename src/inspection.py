"""赛前巡检计划与赛后闭环检查。

赛前：按规则清单逐项检查靶机登记，不合格靶位自动隔离，禁止投入使用。
赛中：由 drift 模块的实时告警与隔离台账承担。
赛后：核对隔离闭环与同步覆盖，形成可归档结论。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .model import RuleSet, SyncSample, Target
from .quarantine import QuarantineLedger, REASON_INSPECTION_FAILED


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    label: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class TargetInspection:
    lane: str
    results: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.results)

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(item for item in self.results if not item.passed)


def build_inspection_plan(rules: RuleSet) -> tuple[dict, ...]:
    """巡检计划来自规则文件，赛前逐项执行。"""
    return rules.inspection_checks


def inspect_target(target: Target, rules: RuleSet) -> TargetInspection:
    labels = {item["id"]: item["label"] for item in rules.inspection_checks}
    th = rules.thresholds

    def result(check_id: str, passed: bool, detail: str) -> CheckResult:
        return CheckResult(check_id, labels.get(check_id, check_id), passed, detail)

    results = [
        result(
            "firmware_approved",
            target.firmware in rules.approved_firmware,
            f"固件 {target.firmware}"
            + (" 在批准清单内" if target.firmware in rules.approved_firmware else " 未获批准"),
        ),
        result(
            "calibration_registered",
            bool(target.calibration_source) and target.calibrated_at is not None,
            "校准源 "
            + (f"{target.calibration_source} 已登记" if target.calibration_source else "未登记"),
        ),
        result(
            "baseline_offset",
            target.baseline_offset_ms is not None
            and abs(target.baseline_offset_ms) < th.critical_offset_ms,
            (
                f"基准偏差 {target.baseline_offset_ms:+.0f} ms"
                if target.baseline_offset_ms is not None
                else "缺少赛前基准偏差"
            ),
        ),
        result(
            "clock_monotonic",
            not target.clock_jump_detected,
            "时钟连续" if not target.clock_jump_detected else "检测到时钟跳变",
        ),
        result(
            "no_open_incidents",
            not target.open_incident,
            "无未闭环事件" if not target.open_incident else "存在未闭环的中断或复位",
        ),
    ]
    return TargetInspection(lane=target.lane, results=tuple(results))


def run_pre_match_inspection(
    targets: list[Target],
    rules: RuleSet,
    ledger: QuarantineLedger,
    at: datetime,
) -> list[TargetInspection]:
    """执行赛前巡检；不合格靶位立即自动隔离，等待整改与官员确认。"""
    inspections = [inspect_target(target, rules) for target in targets]
    for inspection in inspections:
        if not inspection.passed:
            failed = "；".join(item.detail for item in inspection.failures)
            ledger.quarantine(
                inspection.lane,
                REASON_INSPECTION_FAILED,
                at,
                detail=f"赛前巡检不合格：{failed}",
            )
    return inspections


def post_match_audit(
    ledger: QuarantineLedger,
    samples_by_lane: dict[str, list[SyncSample]],
    lanes: list[str],
    rules: RuleSet,
    at: datetime,
) -> tuple[str, ...]:
    """赛后闭环检查：返回发现的问题列表，空列表表示可归档。"""
    findings: list[str] = []
    for event in ledger.open_quarantines(at):
        findings.append(
            f"靶位 {event.lane} 的隔离（{event.detail}）尚未由技术官员确认恢复"
        )
    for lane in lanes:
        samples = samples_by_lane.get(lane, [])
        if not samples:
            findings.append(f"靶位 {lane} 缺少同步样本，无法证明时间基准")
            continue
        ordered = sorted(samples, key=lambda item: item.master_time)
        last = ordered[-1]
        if abs(last.offset_ms) >= rules.thresholds.critical_offset_ms:
            findings.append(
                f"靶位 {lane} 末次同步偏差 {last.offset_ms:+.0f} ms 仍越界"
            )
        gap_s = (at - last.master_time).total_seconds()
        if gap_s > rules.thresholds.max_resync_gap_s:
            findings.append(
                f"靶位 {lane} 赛后 {gap_s / 60:.1f} 分钟无同步样本，末段成绩需复核"
            )
    return tuple(findings)
