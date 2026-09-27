import unittest
from datetime import timedelta

from src.drift import DriftMonitor
from src.inspection import Phase, during_match, post_match, pre_match
from src.models import SyncSample, utc
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


class InspectionTest(unittest.TestCase):
    def test_pre_match_blocks_unregistered_firmware(self):
        monitor = DriftMonitor(RULES)
        monitor.observe(sample("T-1", -600, 5.0))
        report = pre_match(monitor, RULES, ["T-1", "T-2"],
                           {"T-1": "fw-1"}, {"T-1": "GNSS-PRI"}, T0)
        self.assertEqual(report.targets_cleared(), {"T-1"})
        blocked = [r for r in report.failures if r.target_id == "T-2"]
        self.assertTrue(any(r.check_id == "firmware-registered" for r in blocked))
        self.assertTrue(all(r.blocking for r in blocked))

    def test_pre_match_blocks_large_initial_offset(self):
        monitor = DriftMonitor(RULES)
        monitor.observe(sample("T-1", -600, 80.0))  # 初始偏差越告警阈值
        report = pre_match(monitor, RULES, ["T-1"],
                           {"T-1": "fw-1"}, {"T-1": "GNSS-PRI"}, T0)
        self.assertEqual(report.targets_cleared(), set())

    def test_during_match_flags_sampling_gap(self):
        monitor = DriftMonitor(RULES)
        monitor.observe(sample("T-1", 0, 5.0))
        monitor.observe(sample("T-1", 900, 6.0))  # 间隔 900s > 300s
        report = during_match(monitor, RULES, ["T-1"], T0 + timedelta(seconds=900))
        gap = next(r for r in report.results if r.check_id == "sync-interval")
        self.assertFalse(gap.passed)
        self.assertFalse(gap.blocking)  # 赛中为关注项而非阻断项

    def test_post_match_requires_fresh_final_sample(self):
        monitor = DriftMonitor(RULES)
        monitor.observe(sample("T-1", 0, 5.0))
        end = T0 + timedelta(seconds=600)  # 末次采样距结束 600s > 300s
        report = post_match(monitor, RULES, ["T-1"], end)
        fresh = next(r for r in report.results if r.check_id == "final-sync-fresh")
        self.assertFalse(fresh.passed)

    def test_post_match_requires_adjudicated_quarantine(self):
        monitor = DriftMonitor(RULES)
        monitor.observe(sample("T-1", 0, 300.0))  # 自动隔离且未恢复
        end = T0 + timedelta(seconds=240)
        report = post_match(monitor, RULES, ["T-1"], end)
        adj = next(r for r in report.results
                   if r.check_id == "quarantines-adjudicated")
        self.assertFalse(adj.passed)
        # 技术官员确认恢复后通过
        monitor.reinstate("T-1", "TO-203", T0 + timedelta(seconds=120))
        report2 = post_match(monitor, RULES, ["T-1"], end)
        adj2 = next(r for r in report2.results
                    if r.check_id == "quarantines-adjudicated")
        self.assertTrue(adj2.passed)

    def test_phases_cover_pre_during_post(self):
        self.assertEqual({p.value for p in Phase},
                         {"pre_match", "during_match", "post_match"})


if __name__ == "__main__":
    unittest.main()
