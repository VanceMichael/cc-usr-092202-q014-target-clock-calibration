import unittest
from datetime import timedelta

from src.drift import DriftMonitor
from src.models import AlertLevel, ManualReset, SyncSample, utc
from src.rules import Rules

RULES = Rules(
    warn_offset_ms=50.0, isolate_offset_ms=200.0,
    warn_drift_ppm=20.0, isolate_drift_ppm=100.0,
    sync_interval_s=300.0, retransmit_grace_s=120.0,
    final_sync_max_age_s=300.0,
)
T0 = utc("2026-09-27T02:00:00Z")


def sample(tid, seconds, offset_ms):
    ref = T0 + timedelta(seconds=seconds)
    return SyncSample(
        target_id=tid, sampled_at=ref,
        device_time=ref + timedelta(milliseconds=offset_ms),
        reference_time=ref, source_id="GNSS-PRI",
    )


class DriftMonitorTest(unittest.TestCase):
    def test_normal_sample_no_alert(self):
        monitor = DriftMonitor(RULES)
        verdict = monitor.observe(sample("T-1", 0, 10.0))
        self.assertEqual(verdict.alerts, ())
        self.assertFalse(monitor.state("T-1").quarantined)

    def test_warn_then_isolate_escalation(self):
        monitor = DriftMonitor(RULES)
        v1 = monitor.observe(sample("T-1", 0, 80.0))
        self.assertEqual([a.level for a in v1.alerts], [AlertLevel.WARN])
        self.assertFalse(monitor.state("T-1").quarantined)
        v2 = monitor.observe(sample("T-1", 300, 250.0))
        levels = {a.level for a in v2.alerts}
        self.assertIn(AlertLevel.ISOLATE, levels)
        self.assertTrue(monitor.state("T-1").quarantined)
        record = monitor.state("T-1").quarantines[-1]
        self.assertIn("自动隔离", record.reason)

    def test_drift_rate_computed_between_samples(self):
        monitor = DriftMonitor(RULES)
        monitor.observe(sample("T-1", 0, 0.0))
        verdict = monitor.observe(sample("T-1", 100, 10.0))
        # 10ms / 100s = 100 ppm
        self.assertAlmostEqual(verdict.drift_ppm, 100.0)
        self.assertTrue(monitor.state("T-1").quarantined)

    def test_no_duplicate_quarantine_while_open(self):
        monitor = DriftMonitor(RULES)
        monitor.observe(sample("T-1", 0, 300.0))
        monitor.observe(sample("T-1", 60, 400.0))
        self.assertEqual(len(monitor.state("T-1").quarantines), 1)

    def test_reinstate_requires_official(self):
        monitor = DriftMonitor(RULES)
        monitor.observe(sample("T-1", 0, 300.0))
        with self.assertRaises(ValueError):
            monitor.reinstate("T-1", "", T0 + timedelta(minutes=5))
        with self.assertRaises(ValueError):
            monitor.reinstate("T-1", "  ", T0 + timedelta(minutes=5))

    def test_reinstate_records_official_and_time(self):
        monitor = DriftMonitor(RULES)
        monitor.observe(sample("T-1", 0, 300.0))
        at = T0 + timedelta(minutes=10)
        record = monitor.reinstate("T-1", "TO-203", at, "重新授时完成")
        self.assertEqual(record.reinstated_by, "TO-203")
        self.assertEqual(record.reinstated_at, at)
        self.assertFalse(monitor.state("T-1").quarantined)
        # 隔离期覆盖判定：隔离中时刻命中，恢复后不命中
        self.assertIsNotNone(monitor.state("T-1").quarantine_at(
            T0 + timedelta(minutes=5)))
        self.assertIsNone(monitor.state("T-1").quarantine_at(
            T0 + timedelta(minutes=15)))

    def test_reinstate_without_quarantine_rejected(self):
        monitor = DriftMonitor(RULES)
        with self.assertRaises(ValueError):
            monitor.reinstate("T-1", "TO-203", T0)

    def test_manual_reset_rebases_drift(self):
        monitor = DriftMonitor(RULES)
        monitor.observe(sample("T-1", 0, 500.0))  # 越界，隔离
        monitor.record_reset(ManualReset(
            target_id="T-1", reset_at=T0 + timedelta(seconds=50),
            operator="TO-1", reason="更换计时模块"))
        # 复位后偏差回到 2ms：漂移率不得再相对复位前的 500ms 计算
        verdict = monitor.observe(sample("T-1", 100, 2.0))
        self.assertIsNone(verdict.drift_ppm)
        self.assertEqual(verdict.alerts, ())


if __name__ == "__main__":
    unittest.main()
