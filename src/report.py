"""影响范围报告。

综合漂移监测、隔离记录、网络中断、人工复位与重传排序结果，
对每一发给定时间基准判定：

- VALID  偏差在告警阈值内、有前后同步采样支撑、不落在隔离期；
- REVIEW 存在须裁判复核的情形（隔离期发次、乱序冲突、序号缺口、
         偏差告警、采样支撑不足等）；
- HOLD   落在自动隔离期或偏差越隔离阈值，不得直接计入正式成绩。

判定只依据已登记证据追加标注，不改写任何原始命中。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from .drift import DriftMonitor
from .models import NetworkOutage, ShotEvent, fmt
from .reorder import OrderFlag, OrderedShot, ReorderResult
from .rules import Rules


class Basis(Enum):
    VALID = "VALID"    # 有效时间基准，可用于正式成绩
    REVIEW = "REVIEW"  # 须裁判复核
    HOLD = "HOLD"      # 时间基准无效，暂缓认证


@dataclass(frozen=True)
class ShotAssessment:
    shot: OrderedShot
    basis: Basis
    offset_at_shot_ms: Optional[float]
    reasons: tuple[str, ...]

    @property
    def athlete_id(self) -> str:
        return self.shot.event.athlete_id

    @property
    def shot_seq(self) -> int:
        return self.shot.event.shot_seq


@dataclass
class ImpactReport:
    target_id: str
    assessments: list[ShotAssessment] = field(default_factory=list)

    @property
    def review(self) -> list[ShotAssessment]:
        return [a for a in self.assessments if a.basis is Basis.REVIEW]

    @property
    def hold(self) -> list[ShotAssessment]:
        return [a for a in self.assessments if a.basis is Basis.HOLD]

    @property
    def valid(self) -> list[ShotAssessment]:
        return [a for a in self.assessments if a.basis is Basis.VALID]

    def certify_lines(self) -> list[str]:
        """供技术代表签署的正式成绩时间基准结论。"""
        lines = [
            f"靶位 {self.target_id}：有效 {len(self.valid)} 发 / "
            f"须复核 {len(self.review)} 发 / 暂缓 {len(self.hold)} 发",
        ]
        if self.hold:
            lines.append("存在暂缓发次，正式成绩不得整体认证：" +
                         "、".join(f"第{a.shot_seq}发" for a in self.hold))
        elif self.review:
            lines.append("存在待复核发次：" +
                         "、".join(f"第{a.shot_seq}发" for a in self.review) +
                         "；复核裁定前相应发次不作正式纪录依据")
        else:
            lines.append("全部发次采用有效时间基准，可作为正式成绩与纪录认证依据")
        return lines


def estimate_offset_ms(monitor: DriftMonitor, target_id: str,
                       moment: datetime) -> tuple[Optional[float], bool]:
    """估计某时刻设备偏差（ms）。

    用相邻两次同步采样线性插值；落在采样区间之外时用最近样本外推，
    外推结果标记 bracketed=False（支撑不足，须复核）。
    人工复位前后偏差不可比：复位点之后的样本不参与此前时刻的估计，反之亦然。
    """
    state = monitor.state(target_id)
    samples = state.samples
    if not samples:
        return None, False
    resets = [r.reset_at for r in state.resets]

    def same_epoch(sample_time: datetime) -> bool:
        """样本与目标时刻之间不得隔着人工复位。"""
        return not any(min(sample_time, moment) < r <= max(sample_time, moment)
                       for r in resets)

    usable = [s for s in samples if same_epoch(s.reference_time)]
    if not usable:
        return None, False
    before = [s for s in usable if s.reference_time <= moment]
    after = [s for s in usable if s.reference_time >= moment]
    if before and after:
        a, b = before[-1], after[0]
        if a is b:
            return a.offset_ms, True
        span = (b.reference_time - a.reference_time).total_seconds()
        if span == 0:
            return a.offset_ms, True
        ratio = (moment - a.reference_time).total_seconds() / span
        return a.offset_ms + ratio * (b.offset_ms - a.offset_ms), True
    nearest = before[-1] if before else after[0]
    return nearest.offset_ms, False


def _in_outage(shot: ShotEvent, outages: list[NetworkOutage]) -> Optional[NetworkOutage]:
    for outage in outages:
        end = outage.recovered_at or shot.received_at
        # 设备时间落在中断区间，或该发在中断恢复后才到达
        if outage.started_at <= shot.device_time <= end:
            return outage
        if shot.received_at > outage.started_at and (
            outage.recovered_at is None or shot.received_at >= outage.recovered_at
        ) and shot.device_time <= end:
            return outage
    return None


def assess_target(monitor: DriftMonitor, rules: Rules, reordered: ReorderResult,
                  outages: list[NetworkOutage]) -> ImpactReport:
    """对一个靶位的规范顺序逐发做时间基准判定。"""
    report = ImpactReport(target_id=reordered.target_id)
    for ordered in reordered.ordered:
        ev = ordered.event
        reasons: list[str] = []
        basis = Basis.VALID

        quarantine = monitor.state(ev.target_id).quarantine_at(ev.device_time)
        offset, bracketed = estimate_offset_ms(monitor, ev.target_id, ev.device_time)

        if quarantine is not None:
            reasons.append(f"发次落在隔离期（{quarantine.reason}）")
            basis = Basis.HOLD
        elif offset is not None and abs(offset) >= rules.isolate_offset_ms:
            reasons.append(f"发次时刻估计偏差 {offset:+.1f}ms 越隔离阈值")
            basis = Basis.HOLD

        if basis is not Basis.HOLD and offset is not None \
                and abs(offset) >= rules.warn_offset_ms:
            reasons.append(f"发次时刻估计偏差 {offset:+.1f}ms 越告警阈值")
            basis = Basis.REVIEW
        if not bracketed:
            reasons.append("发次时刻无前后同步采样夹持，基准支撑不足")
            if basis is Basis.VALID:
                basis = Basis.REVIEW

        if OrderFlag.CONFLICT in ordered.flags:
            reasons.append("同一事件存在内容矛盾的重复上传")
            basis = Basis.REVIEW
        if OrderFlag.SEQUENCE_GAP in ordered.flags:
            reasons.append("发次序号存在缺口，可能有未送达事件")
            basis = Basis.REVIEW
        if OrderFlag.LATE_RETRANSMIT in ordered.flags:
            reasons.append("超出重传宽限的补传，到达时序须复核")
            if basis is Basis.VALID:
                basis = Basis.REVIEW
        if OrderFlag.OUT_OF_ORDER in ordered.flags and basis is Basis.VALID:
            reasons.append("到达顺序与发次序号不一致（已按序号规范排序）")

        outage = _in_outage(ev, outages)
        if outage is not None:
            reasons.append(
                f"发生于网络中断区间（{fmt(outage.started_at)}–"
                f"{fmt(outage.recovered_at)}），经缓存补传"
            )

        if not reasons:
            reasons.append("偏差在阈值内，前后同步采样夹持，时间基准有效")
        report.assessments.append(ShotAssessment(
            shot=ordered, basis=basis,
            offset_at_shot_ms=offset, reasons=tuple(reasons),
        ))
    return report
