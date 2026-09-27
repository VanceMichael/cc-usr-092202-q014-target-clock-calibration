import unittest
from datetime import timedelta
from pathlib import Path

from src.drift import DriftMonitor
from src.models import utc
from src.report import Basis, assess_target, estimate_offset_ms
from src.reorder import reorder
from src.rules import Rules
from src.scenario import load_scenario

BASE = Path(__file__).resolve().parent.parent


class ScenarioPipelineTest(unittest.TestCase):
    """用公开样例走完整流程，验证影响范围报告的关键结论。"""

    @classmethod
    def setUpClass(cls):
        cls.rules = Rules.load(BASE / "fixtures" / "rules.json")
        cls.sc = load_scenario(BASE / "fixtures" / "scenario.json")
        cls.monitor = DriftMonitor(cls.rules)
        timeline = [(s.sampled_at, "s", s) for s in cls.sc.sync_samples]
        timeline += [(r.reset_at, "r", r) for r in cls.sc.resets]
        timeline += [(utc(ri["at"]), "i", ri) for ri in cls.sc.reinstatements]
        for at, kind, payload in sorted(timeline, key=lambda x: x[0]):
            if kind == "s":
                cls.monitor.observe(payload)
            elif kind == "r":
                cls.monitor.record_reset(payload)
            else:
                cls.monitor.reinstate(payload["target_id"], payload["official_id"],
                                      at, payload["note"])
        cls.impacts = {}
        for tid in cls.sc.target_ids:
            events = [s for s in cls.sc.shots if s.target_id == tid]
            res = reorder(events, tid, retransmit_grace_s=cls.rules.retransmit_grace_s)
            outages = [o for o in cls.sc.outages if o.target_id == tid]
            cls.impacts[tid] = assess_target(cls.monitor, cls.rules, res, outages)

    def test_healthy_target_all_valid(self):
        impact = self.impacts["T-07"]
        self.assertEqual(len(impact.valid), 3)
        self.assertEqual(impact.review, [])
        self.assertEqual(impact.hold, [])

    def test_drift_target_isolated_shot_on_hold(self):
        impact = self.impacts["T-09"]
        by_seq = {a.shot_seq: a for a in impact.assessments}
        self.assertIs(by_seq[1].basis, Basis.VALID)
        self.assertIs(by_seq[2].basis, Basis.REVIEW)   # 越告警阈值
        self.assertIs(by_seq[3].basis, Basis.HOLD)     # 落在隔离期
        self.assertIs(by_seq[4].basis, Basis.VALID)    # 复位恢复后
        # 暂缓发次阻断整体认证
        self.assertTrue(any("不得整体认证" in line
                            for line in impact.certify_lines()))

    def test_outage_target_retransmit_review(self):
        impact = self.impacts["T-12"]
        by_seq = {a.shot_seq: a for a in impact.assessments}
        self.assertIs(by_seq[1].basis, Basis.VALID)
        for seq in (2, 3, 4):  # 中断补传
            self.assertIs(by_seq[seq].basis, Basis.REVIEW)
        self.assertIs(by_seq[5].basis, Basis.REVIEW)   # 矛盾上传
        self.assertIs(by_seq[6].basis, Basis.REVIEW)   # 序号缺口
        self.assertIs(by_seq[8].basis, Basis.VALID)
        # 第 7 发缺失
        self.assertNotIn(7, by_seq)

    def test_offset_estimation_respects_manual_reset(self):
        # 复位前时刻用复位前样本外推，复位后时刻用复位后样本插值
        before, bracketed_before = estimate_offset_ms(
            self.monitor, "T-09", utc("2026-09-27T02:55:00Z"))
        after, bracketed_after = estimate_offset_ms(
            self.monitor, "T-09", utc("2026-09-27T03:15:00Z"))
        self.assertGreater(before, 200.0)          # 复位前漂移已超过隔离阈值
        self.assertFalse(bracketed_before)          # 复位点截断，无夹持
        self.assertAlmostEqual(after, -2.0, places=1)
        self.assertTrue(bracketed_after)

    def test_quarantine_requires_official_reinstatement(self):
        state = self.monitor.state("T-09")
        self.assertEqual(len(state.quarantines), 1)
        record = state.quarantines[0]
        self.assertEqual(record.reinstated_by, "TO-203")
        self.assertIsNotNone(record.reinstated_at)


if __name__ == "__main__":
    unittest.main()
