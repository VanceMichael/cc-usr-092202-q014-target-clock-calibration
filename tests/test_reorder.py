import unittest
from datetime import timedelta

from src.models import ShotEvent, utc
from src.reorder import OrderFlag, reorder

T0 = utc("2026-09-27T02:00:00Z")


def shot(eid, seq, dev_s, recv_s, score=10.0, athlete="A-1"):
    return ShotEvent(
        event_id=eid, target_id="T-1", athlete_id=athlete, relay=1,
        shot_seq=seq, device_time=T0 + timedelta(seconds=dev_s),
        received_at=T0 + timedelta(seconds=recv_s), score=score,
    )


class ReorderTest(unittest.TestCase):
    def test_canonical_order_by_shot_seq(self):
        events = [shot("e3", 3, 30, 31), shot("e1", 1, 10, 11), shot("e2", 2, 20, 21)]
        result = reorder(events, "T-1")
        self.assertEqual([s.event.shot_seq for s in result.ordered], [1, 2, 3])
        self.assertEqual(result.gaps, [])

    def test_identical_duplicate_discarded_original_kept(self):
        first = shot("e1", 1, 10, 11)
        dup = shot("e1", 1, 10, 900)  # 中断恢复后原样重传
        result = reorder([first, dup], "T-1")
        self.assertEqual(len(result.ordered), 1)
        self.assertEqual(result.discarded_duplicates, [dup])
        self.assertIn(OrderFlag.DUPLICATE, result.ordered[0].flags)
        # 保留的是先到达的原始事件
        self.assertIs(result.ordered[0].event, first)

    def test_conflicting_uploads_flagged_not_rewritten(self):
        a = shot("e1", 1, 10, 11, score=10.4)
        b = shot("e1", 1, 10, 12, score=9.8)  # 同一事件环值矛盾
        result = reorder([a, b], "T-1")
        self.assertEqual(len(result.ordered), 1)
        kept = result.ordered[0]
        self.assertIn(OrderFlag.CONFLICT, kept.flags)
        self.assertTrue(kept.needs_review)
        self.assertEqual(kept.event.score, 10.4)  # 保留先到达者，不改写

    def test_out_of_order_flagged_by_first_arrival(self):
        # 两发到达顺序与发次序号互换：两者的到达位次均与规范位次不一致
        events = [shot("e2", 2, 20, 21), shot("e1", 1, 10, 22)]
        result = reorder(events, "T-1")
        self.assertEqual([s.event.shot_seq for s in result.ordered], [1, 2])
        for s in result.ordered:
            self.assertIn(OrderFlag.OUT_OF_ORDER, s.flags)
        # 顺序一致的到达不标乱序
        ordered_events = [shot("e1", 1, 10, 11), shot("e2", 2, 20, 21)]
        result2 = reorder(ordered_events, "T-1")
        for s in result2.ordered:
            self.assertNotIn(OrderFlag.OUT_OF_ORDER, s.flags)

    def test_late_retransmit_flag(self):
        early = shot("e1", 1, 10, 11)
        late = shot("e2", 2, 20, 20 + 200)  # 到达滞后 200s > 宽限 120s
        result = reorder([early, late], "T-1", retransmit_grace_s=120.0)
        self.assertNotIn(OrderFlag.LATE_RETRANSMIT, result.ordered[0].flags)
        self.assertIn(OrderFlag.LATE_RETRANSMIT, result.ordered[1].flags)

    def test_sequence_gap_detected_and_flagged(self):
        events = [shot("e1", 1, 10, 11), shot("e3", 3, 30, 31)]
        result = reorder(events, "T-1")
        self.assertEqual(result.gaps, [2])
        self.assertIn(OrderFlag.SEQUENCE_GAP, result.ordered[0].flags)
        self.assertTrue(result.ordered[0].needs_review)


if __name__ == "__main__":
    unittest.main()
