"""影响范围报告与纪录时间基准声明。

汇总赛前巡检、赛中告警与隔离、离线重传重建、逐发时间基准判定，
输出裁判复核清单与技术代表可归档的认证声明。只读，不改写任何原始数据。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .drift import Alert
from .inspection import TargetInspection
from .model import Claim, RuleSet, fmt_ts
from .quarantine import QuarantineEvent
from .reorder import LaneReconstruction
from .timebasis import STATUS_INVALID, STATUS_REVIEW, STATUS_VALID, ShotBasis


@dataclass(frozen=True)
class ShotReviewItem:
    lane: str
    shot_seq: int
    device_time: str
    received_time: str
    score: float
    status: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class LaneImpact:
    lane: str
    status: str  # clear / review / blocked
    total_shots: int
    valid_shots: int
    review_shots: int
    invalid_shots: int
    duplicates: int
    out_of_order: int
    delayed: int
    missing_seqs: tuple[int, ...]
    quarantine_periods: tuple[dict, ...]


@dataclass(frozen=True)
class ClaimAttestation:
    claim_id: str
    label: str
    lane: str
    athlete: str
    claimed_total: float
    recomputed_total: float
    total_matches: bool
    shot_basis_ok: bool
    certified: bool
    notes: tuple[str, ...]


@dataclass(frozen=True)
class ImpactReport:
    match_id: str
    match_name: str
    generated_at: datetime
    inspection: tuple[TargetInspection, ...]
    alerts: tuple[Alert, ...]
    quarantine_events: tuple[QuarantineEvent, ...]
    lanes: tuple[LaneImpact, ...]
    review_items: tuple[ShotReviewItem, ...]
    attestations: tuple[ClaimAttestation, ...]
    audit_findings: tuple[str, ...]


def build_lane_impact(
    lane: str,
    bases: list[ShotBasis],
    reconstruction: LaneReconstruction,
    quarantine_events: tuple[QuarantineEvent, ...],
) -> LaneImpact:
    valid = sum(1 for basis in bases if basis.status == STATUS_VALID)
    review = sum(1 for basis in bases if basis.status == STATUS_REVIEW)
    invalid = sum(1 for basis in bases if basis.status == STATUS_INVALID)
    open_quarantine = any(
        event.lane == lane and event.recovered_at is None
        for event in quarantine_events
    )
    if invalid > 0 or open_quarantine:
        status = "blocked"
    elif review > 0:
        status = "review"
    else:
        status = "clear"
    periods = tuple(
        {
            "reason": event.reason,
            "detail": event.detail,
            "quarantined_at": fmt_ts(event.quarantined_at),
            "recovered_at": fmt_ts(event.recovered_at) if event.recovered_at else None,
            "recovered_by": event.recovered_by,
        }
        for event in quarantine_events
        if event.lane == lane
    )
    return LaneImpact(
        lane=lane,
        status=status,
        total_shots=len(bases),
        valid_shots=valid,
        review_shots=review,
        invalid_shots=invalid,
        duplicates=reconstruction.duplicate_count,
        out_of_order=reconstruction.out_of_order_count,
        delayed=reconstruction.delayed_count,
        missing_seqs=reconstruction.missing_seqs,
        quarantine_periods=periods,
    )


def build_review_items(bases_by_lane: dict[str, list[ShotBasis]]) -> tuple[ShotReviewItem, ...]:
    items: list[ShotReviewItem] = []
    for lane in sorted(bases_by_lane):
        for basis in bases_by_lane[lane]:
            if basis.status == STATUS_VALID:
                continue
            items.append(ShotReviewItem(
                lane=basis.event.lane,
                shot_seq=basis.event.shot_seq,
                device_time=fmt_ts(basis.event.device_time),
                received_time=fmt_ts(basis.event.received_time),
                score=basis.event.score,
                status=basis.status,
                reasons=basis.reason_labels,
            ))
    return tuple(items)


def attest_claim(
    claim: Claim,
    bases: list[ShotBasis],
    inspection_passed: bool,
) -> ClaimAttestation:
    notes: list[str] = []
    primary = [basis for basis in bases if basis.status != STATUS_INVALID]
    valid = [basis for basis in bases if basis.status == STATUS_VALID]
    review = [basis for basis in bases if basis.status == STATUS_REVIEW]

    recomputed = round(sum(basis.event.score for basis in valid), 1)
    total_matches = abs(recomputed - claim.claimed_total) < 0.05

    basis_ok = (
        inspection_passed
        and len(valid) == claim.shot_count
        and not review
        and len(primary) == claim.shot_count
    )
    if not inspection_passed:
        notes.append("靶位赛前巡检未全部通过")
    if review:
        notes.append(f"仍有 {len(review)} 发待复核，未采用有效时间基准")
    if len(valid) != claim.shot_count:
        notes.append(f"有效时间基准发数 {len(valid)} 与申报 {claim.shot_count} 不一致")
    if not total_matches:
        notes.append(f"有效发次合计 {recomputed} 与申报总成绩 {claim.claimed_total} 不一致")

    certified = basis_ok and total_matches
    if certified:
        notes.append("全部发次采用有效时间基准，总成绩与申报一致，可用于纪录认证")

    return ClaimAttestation(
        claim_id=claim.id,
        label=claim.label,
        lane=claim.lane,
        athlete=claim.athlete,
        claimed_total=claim.claimed_total,
        recomputed_total=recomputed,
        total_matches=total_matches,
        shot_basis_ok=basis_ok,
        certified=certified,
        notes=tuple(notes),
    )


def render_text(report: ImpactReport, rules: RuleSet) -> str:
    """中文可读报告：裁判看复核清单，技术代表看认证声明。"""
    lines: list[str] = []
    lines.append(f"# 电子靶时钟校验报告 — {report.match_name}")
    lines.append(f"赛事编号：{report.match_id}；生成时间：{fmt_ts(report.generated_at)}")
    lines.append("")

    lines.append("## 一、赛前巡检")
    for inspection in report.inspection:
        mark = "通过" if inspection.passed else "不合格"
        lines.append(f"- 靶位 {inspection.lane}：{mark}")
        for failure in inspection.failures:
            lines.append(f"    - {failure.label}：{failure.detail}")
    lines.append("")

    lines.append("## 二、赛中实时告警")
    if not report.alerts:
        lines.append("- 无告警")
    for alert in report.alerts:
        lines.append(
            f"- [{alert.level.upper()}] {fmt_ts(alert.at)} 靶位 {alert.lane} "
            f"{alert.code}：{alert.message}"
        )
    lines.append("")

    lines.append("## 三、隔离与恢复台账")
    if not report.quarantine_events:
        lines.append("- 无隔离记录")
    for event in report.quarantine_events:
        if event.recovered_at:
            recovery = f"恢复于 {fmt_ts(event.recovered_at)}，确认人：{event.recovered_by}"
        else:
            recovery = "未恢复"
        lines.append(
            f"- 靶位 {event.lane}：{event.detail}；隔离于 "
            f"{fmt_ts(event.quarantined_at)}；{recovery}"
        )
    lines.append("")

    lines.append("## 四、各靶位影响范围")
    for lane in report.lanes:
        lines.append(
            f"- 靶位 {lane.lane}（{lane.status}）：共 {lane.total_shots} 发，"
            f"有效 {lane.valid_shots}，待复核 {lane.review_shots}，无效 {lane.invalid_shots}；"
            f"重复报文 {lane.duplicates}，乱序 {lane.out_of_order}，补传 {lane.delayed}"
        )
        if lane.missing_seqs:
            lines.append(f"    - 缺失发次号：{list(lane.missing_seqs)}")
    lines.append("")

    lines.append("## 五、需要复核的发次")
    if not report.review_items:
        lines.append("- 无")
    for item in report.review_items:
        lines.append(
            f"- 靶位 {item.lane} 第 {item.shot_seq} 发（{item.score} 环，"
            f"设备时间 {item.device_time}，接收时间 {item.received_time}）："
            f"{'；'.join(item.reasons)}"
        )
    lines.append("")

    lines.append("## 六、正式成绩时间基准声明")
    for att in report.attestations:
        verdict = "可以认证" if att.certified else "暂不能认证"
        lines.append(
            f"- {att.label}（{att.athlete}，靶位 {att.lane}）：{verdict}。"
            f"申报总成绩 {att.claimed_total}，有效发次合计 {att.recomputed_total}"
        )
        for note in att.notes:
            lines.append(f"    - {note}")
    lines.append("")

    lines.append("## 七、赛后闭环检查")
    if not report.audit_findings:
        lines.append("- 全部闭环，可归档")
    for finding in report.audit_findings:
        lines.append(f"- {finding}")
    lines.append("")
    lines.append("说明：本服务只读原始命中报文（设备时间与接收时间双时间戳），"
                 "不修改任何原始成绩；隔离由系统自动执行，恢复使用必须经技术官员确认。")
    return "\n".join(lines)
