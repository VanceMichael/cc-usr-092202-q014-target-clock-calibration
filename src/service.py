"""时钟校验服务编排：赛前巡检 → 赛中实时告警/隔离 → 离线重传 → 赛后报告。

数据流（均为只读样例文件）：
rules/targets/telemetry/claims → 校验、隔离台账、重建视图、时间基准判定、影响报告。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import drift
from .impact import (
    ImpactReport,
    attest_claim,
    build_lane_impact,
    build_review_items,
    render_text,
)
from .inspection import post_match_audit, run_pre_match_inspection
from .model import (
    Claim,
    Incident,
    Reinstatement,
    RuleSet,
    ScoreEvent,
    SyncSample,
    Target,
    parse_ts,
)
from .quarantine import (
    QuarantineLedger,
    REASON_MANUAL_RESET,
    REASON_OFFSET_CRITICAL,
)
from .reorder import reconstruct_all
from .timebasis import classify_lane

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class ClockCalibrationService:
    """聚合全部靶位的校验过程，保留台账与判定结果。"""

    def __init__(self, fixture_dir: Path = FIXTURE_DIR) -> None:
        self.fixture_dir = fixture_dir
        self.rules = RuleSet.from_dict(_load(fixture_dir / "rules.json"))
        self.targets = [Target.from_dict(item) for item in _load(fixture_dir / "targets.json")["targets"]]
        telemetry = _load(fixture_dir / "telemetry.json")
        self.samples = [SyncSample.from_dict(item) for item in telemetry["sync_samples"]]
        self.incidents = [Incident.from_dict(item) for item in telemetry["incidents"]]
        self.reinstatements_data = [
            Reinstatement.from_dict(item) for item in telemetry.get("reinstatements", [])
        ]
        self.events = [ScoreEvent.from_dict(item) for item in telemetry["score_events"]]
        self.claims = [Claim.from_dict(item) for item in _load(fixture_dir / "claims.json")["claims"]]

        self.ledger = QuarantineLedger()
        self.lanes = [target.lane for target in self.targets]
        self.inspections: list = []
        self.alerts: list = []
        self._monitored = False
        self._first_critical: dict[str, datetime] = {}

    # ---- 赛前 ----
    def pre_match(self):
        self.inspections = run_pre_match_inspection(
            self.targets, self.rules, self.ledger, self.rules.inspection_time
        )
        return self.inspections

    # ---- 赛中 ----
    def monitor(self):
        """按时间顺序消费同步样本：评估漂移、越界自动隔离、官员确认后恢复。"""
        samples_by_lane: dict[str, list[SyncSample]] = {}
        for item in self.samples:
            samples_by_lane.setdefault(item.lane, []).append(item)

        for lane in self.lanes:
            points, alerts, first_critical = drift.assess_lane(
                lane, samples_by_lane.get(lane, []), self.rules,
                incidents=self.incidents,
            )
            self.alerts.extend(alerts)
            if first_critical is not None:
                self._first_critical[lane] = first_critical
                self.ledger.quarantine(
                    lane,
                    REASON_OFFSET_CRITICAL,
                    first_critical,
                    detail="实时同步偏差越出严重限值，系统自动隔离",
                )

        # 人工复位登记：复位后即隔离，等待技术官员重新同步并确认
        for incident in self.incidents:
            if incident.kind == "manual_reset":
                self.ledger.quarantine(
                    incident.lane,
                    REASON_MANUAL_RESET,
                    incident.start,
                    detail=f"人工复位：{incident.note}",
                )

        # 恢复使用只能来自技术官员的确认登记
        for reinstatement in sorted(self.reinstatements_data, key=lambda item: item.confirmed_at):
            self.ledger.reinstate(
                reinstatement.lane,
                reinstatement.officer,
                reinstatement.confirmed_at,
                note=reinstatement.note,
            )

        self._monitored = True
        return self.alerts

    # ---- 赛后 ----
    def build_report(self, generated_at: datetime | None = None) -> ImpactReport:
        if not self.inspections:
            self.pre_match()
        if not self._monitored:
            self.monitor()

        samples_by_lane: dict[str, list[SyncSample]] = {}
        for item in self.samples:
            samples_by_lane.setdefault(item.lane, []).append(item)

        reconstructions = reconstruct_all(self.events, self.lanes)
        bases_by_lane: dict[str, list] = {}
        for lane in self.lanes:
            reconstruction = reconstructions[lane]
            # 时间基准判定基于去重后的有效报文（原始报文仍完整保留）
            primary_events = [item.event for item in reconstruction.primary_shots]
            bases_by_lane[lane] = classify_lane(
                lane,
                primary_events,
                self.ledger,
                samples_by_lane.get(lane, []),
                self.incidents,
                self.rules,
                seq_conflicts=reconstruction.seq_conflicts,
            )

        lane_impacts = tuple(
            build_lane_impact(
                lane,
                bases_by_lane[lane],
                reconstructions[lane],
                self.ledger.events,
            )
            for lane in self.lanes
        )

        inspection_passed = {
            inspection.lane: inspection.passed for inspection in self.inspections
        }
        attestations = tuple(
            attest_claim(
                claim,
                bases_by_lane.get(claim.lane, []),
                inspection_passed.get(claim.lane, False),
            )
            for claim in self.claims
        )

        audit_findings = post_match_audit(
            self.ledger, samples_by_lane, self.lanes, self.rules, self.rules.end
        )

        return ImpactReport(
            match_id=self.rules.match_id,
            match_name=self.rules.match_name,
            generated_at=generated_at or self.rules.end,
            inspection=tuple(self.inspections),
            alerts=tuple(self.alerts),
            quarantine_events=self.ledger.events,
            lanes=lane_impacts,
            review_items=build_review_items(bases_by_lane),
            attestations=attestations,
            audit_findings=audit_findings,
        )

    def run_text(self) -> str:
        if not self.inspections:
            self.pre_match()
        if not self._monitored:
            self.monitor()
        return render_text(self.build_report(), self.rules)


def main() -> None:
    print(ClockCalibrationService().run_text())


if __name__ == "__main__":
    main()
