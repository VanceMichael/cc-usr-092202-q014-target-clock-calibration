"""离线重传排序：按设备时间重建逐发顺序，去重、标记乱序与补传。

只生成重建视图，绝不修改原始成绩事件。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .model import ScoreEvent

DEFAULT_DELAY_THRESHOLD_S = 60.0


@dataclass(frozen=True)
class OrderedShot:
    """重建视图中的一发：指向原始事件并附排序标记。"""

    event: ScoreEvent
    is_duplicate: bool
    is_out_of_order: bool
    is_delayed: bool


@dataclass(frozen=True)
class LaneReconstruction:
    lane: str
    ordered: tuple[OrderedShot, ...]
    duplicate_count: int
    out_of_order_count: int
    delayed_count: int
    missing_seqs: tuple[int, ...]
    seq_conflicts: tuple[int, ...]

    @property
    def primary_shots(self) -> tuple[OrderedShot, ...]:
        """去重后的有效发次序列（每发保留最早到达的原始报文）。"""
        return tuple(item for item in self.ordered if not item.is_duplicate)


def reconstruct_lane(
    lane: str,
    events: list[ScoreEvent],
    delay_threshold_s: float = DEFAULT_DELAY_THRESHOLD_S,
) -> LaneReconstruction:
    raw = [event for event in events if event.lane == lane]

    # 去重：设备时间、发次号、环值一致视为同一报文的重传，保留最早到达者
    first_seen: dict[tuple, ScoreEvent] = {}
    duplicate_ids: set[int] = set()
    for event in raw:
        key = (event.shot_seq, event.device_time, event.score)
        existing = first_seen.get(key)
        if existing is None or event.received_time < existing.received_time:
            if existing is not None:
                duplicate_ids.add(id(existing))
            first_seen[key] = event
        else:
            duplicate_ids.add(id(event))

    # 同一发次号但内容不一致：冲突，全部保留并标记，交由裁判裁定
    by_seq: dict[int, set] = {}
    for event in raw:
        by_seq.setdefault(event.shot_seq, set()).add((event.device_time, event.score))
    seq_conflicts = tuple(sorted(seq for seq, variants in by_seq.items() if len(variants) > 1))

    # 乱序标记：到达顺序与设备时间顺序不一致（离线补传的典型特征）
    max_device_seen: datetime | None = None
    out_of_order_ids: set[int] = set()
    for event in raw:
        if max_device_seen is not None and event.device_time < max_device_seen:
            out_of_order_ids.add(id(event))
        if max_device_seen is None or event.device_time > max_device_seen:
            max_device_seen = event.device_time

    ordered_raw = sorted(raw, key=lambda item: (item.device_time, item.shot_seq, item.received_time))
    ordered = tuple(
        OrderedShot(
            event=event,
            is_duplicate=id(event) in duplicate_ids,
            is_out_of_order=id(event) in out_of_order_ids,
            is_delayed=event.delay_s > delay_threshold_s,
        )
        for event in ordered_raw
    )

    primary_seqs = sorted(
        {item.event.shot_seq for item in ordered if not item.is_duplicate}
    )
    missing: list[int] = []
    if primary_seqs:
        present = set(primary_seqs)
        missing = [seq for seq in range(min(present), max(present) + 1) if seq not in present]

    return LaneReconstruction(
        lane=lane,
        ordered=ordered,
        duplicate_count=sum(1 for item in ordered if item.is_duplicate),
        out_of_order_count=sum(1 for item in ordered if item.is_out_of_order),
        delayed_count=sum(1 for item in ordered if item.is_delayed),
        missing_seqs=tuple(missing),
        seq_conflicts=seq_conflicts,
    )


def reconstruct_all(
    events: list[ScoreEvent],
    lanes: list[str],
    delay_threshold_s: float = DEFAULT_DELAY_THRESHOLD_S,
) -> dict[str, LaneReconstruction]:
    return {
        lane: reconstruct_lane(lane, events, delay_threshold_s)
        for lane in lanes
    }
