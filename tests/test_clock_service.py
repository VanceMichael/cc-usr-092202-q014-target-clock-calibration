import dataclasses
import unittest
from pathlib import Path

from src.drift import assess_lane
from src.inspection import build_inspection_plan
from src.model import ScoreEvent, parse_ts
from src.quarantine import QuarantineLedger, REASON_OFFSET_CRITICAL
from src.reorder import reconstruct_lane
from src.service import ClockCalibrationService, FIXTURE_DIR
from src.timebasis import STATUS_REVIEW, STATUS_VALID


def make_service() -> ClockCalibrationService:
    service = ClockCalibrationService(FIXTURE_DIR)
    service.pre_match()
    service.monitor()
    return service


class InspectionTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        self.by_lane = {item.lane: item for item in self.service.inspections}

    def test_plan_comes_from_rules(self):
        plan = build_inspection_plan(self.service.rules)
        self.assertEqual(len(plan), 5)
        self.assertIn("firmware_approved", {item["id"] for item in plan})

    def test_healthy_targets_pass(self):
        for lane in ("A-01", "A-02", "A-03"):
            self.assertTrue(self.by_lane[lane].passed, lane)

    def test_unregistered_target_blocked(self):
        inspection = self.by_lane["A-04"]
        self.assertFalse(inspection.passed)
        failed_ids = {item.check_id for item in inspection.failures}
        self.assertIn("firmware_approved", failed_ids)
        self.assertIn("calibration_registered", failed_ids)
        self.assertIn("clock_monotonic", failed_ids)
        # 不合格靶位在赛前即被隔离，禁止投入使用
        self.assertTrue(
            self.service.ledger.is_quarantined(
                "A-04", self.service.rules.inspection_time
            )
        )


class DriftAlertTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        self.alerts = self.service.alerts

    def codes(self, lane):
        return [alert.code for alert in self.alerts if alert.lane == lane]

    def test_critical_offset_raises_alert_and_quarantine(self):
        codes = self.codes("A-02")
        self.assertIn("OFFSET_WARN", codes)
        self.assertIn("OFFSET_CRITICAL", codes)
        self.assertIn("DRIFT_CRITICAL", codes)
        # 越界时刻自动隔离
        self.assertTrue(
            self.service.ledger.is_quarantined("A-02", parse_ts("2026-09-27T09:40:00.000Z"))
        )

    def test_sync_gap_alerted_after_outage(self):
        self.assertIn("SYNC_GAP", self.codes("A-02"))

    def test_healthy_lane_has_no_offset_alert(self):
        codes = self.codes("A-01")
        self.assertNotIn("OFFSET_WARN", codes)
        self.assertNotIn("OFFSET_CRITICAL", codes)

    def test_recovery_is_info_only(self):
        recovered = [a for a in self.alerts if a.code == "OFFSET_RECOVERED"]
        self.assertTrue(recovered)
        self.assertTrue(all(a.level == "info" for a in recovered))


class QuarantineLifecycleTest(unittest.TestCase):
    def test_manual_reset_quarantines_until_officer_confirms(self):
        service = make_service()
        # 复位后、确认前处于隔离
        self.assertTrue(service.ledger.is_quarantined("A-03", parse_ts("2026-09-27T09:35:00.000Z")))
        # 技术官员确认后恢复
        self.assertFalse(service.ledger.is_quarantined("A-03", parse_ts("2026-09-27T09:45:00.000Z")))

    def test_reinstate_requires_officer(self):
        ledger = QuarantineLedger()
        at = parse_ts("2026-09-27T09:35:00.000Z")
        ledger.quarantine("A-09", REASON_OFFSET_CRITICAL, at)
        with self.assertRaises(PermissionError):
            ledger.reinstate("A-09", "", at)
        with self.assertRaises(ValueError):
            ledger.reinstate("A-08", "技术官员-丁", at)  # 未隔离的靶位

    def test_no_auto_reinstatement(self):
        service = make_service()
        # A-04 没有任何官员确认登记，赛后仍处隔离
        self.assertTrue(
            service.ledger.is_quarantined("A-04", service.rules.end)
        )
        recovered = [e for e in service.ledger.events if e.recovered_at]
        self.assertTrue(all(e.recovered_by for e in recovered))


class ReorderTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        self.reconstruction = reconstruct_lane(
            "A-02", self.service.events
        )

    def test_canonical_order_by_device_time(self):
        seqs = [item.event.shot_seq for item in self.reconstruction.primary_shots]
        self.assertEqual(seqs, [12, 13, 14, 15, 16, 17, 18])

    def test_duplicate_retransmission_detected(self):
        self.assertEqual(self.reconstruction.duplicate_count, 1)
        # 原始报文一条不少，重复只是视图标记
        raw_count = sum(1 for e in self.service.events if e.lane == "A-02")
        self.assertEqual(raw_count, 8)
        self.assertEqual(len(self.reconstruction.primary_shots), 7)

    def test_out_of_order_and_delayed_flagged(self):
        self.assertEqual(self.reconstruction.out_of_order_count, 3)
        self.assertGreaterEqual(self.reconstruction.delayed_count, 4)

    def test_raw_events_immutable(self):
        event = next(e for e in self.service.events if e.lane == "A-02")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            event.score = 0.0  # type: ignore[misc]


class TimeBasisTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        self.report = self.service.build_report()
        self.review = {(item.lane, item.shot_seq): item for item in self.report.review_items}

    def test_record_lane_all_valid(self):
        lane = next(item for item in self.report.lanes if item.lane == "A-01")
        self.assertEqual(lane.status, "clear")
        self.assertEqual(lane.valid_shots, 60)
        self.assertEqual(lane.review_shots, 0)

    def test_quarantined_and_offline_shots_need_review(self):
        for seq in (12, 13, 14, 15, 16, 17):
            self.assertIn(("A-02", seq), self.review)
        self.assertNotIn(("A-02", 18), self.review)

    def test_manual_reset_shot_needs_review(self):
        self.assertIn(("A-03", 6), self.review)
        self.assertNotIn(("A-03", 5), self.review)
        self.assertNotIn(("A-03", 7), self.review)

    def test_blocked_lane_reported(self):
        lane = next(item for item in self.report.lanes if item.lane == "A-04")
        self.assertEqual(lane.status, "blocked")


class AttestationTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        self.report = self.service.build_report()

    def test_record_claim_certified(self):
        attestation = self.report.attestations[0]
        self.assertTrue(attestation.certified)
        self.assertTrue(attestation.total_matches)
        self.assertEqual(attestation.recomputed_total, 630.6)
        self.assertEqual(attestation.claimed_total, 630.6)

    def test_tampered_claim_rejected(self):
        service = make_service()
        claim = service.claims[0]
        tampered = dataclasses.replace(claim, claimed_total=999.9)
        service.claims = [tampered]
        report = service.build_report()
        self.assertFalse(report.attestations[0].certified)
        self.assertFalse(report.attestations[0].total_matches)

    def test_audit_closed_with_reinstatements(self):
        # 样例中 A-02/A-03 均已由技术官员确认恢复；仅剩 A-04 未整改的提示
        for finding in self.report.audit_findings:
            self.assertIn("A-04", finding)

    def test_audit_flags_missing_reinstatement(self):
        service = ClockCalibrationService(FIXTURE_DIR)
        service.reinstatements_data = [
            item for item in service.reinstatements_data if item.lane != "A-02"
        ]
        report = service.build_report()
        self.assertTrue(any("A-02" in finding for finding in report.audit_findings))

    def test_report_text_mentions_review_and_attestation(self):
        text = self.service.run_text()
        self.assertIn("需要复核的发次", text)
        self.assertIn("时间基准声明", text)
        self.assertIn("可以认证", text)


if __name__ == "__main__":
    unittest.main()
