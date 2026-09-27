"""离线重传排序。

网络中断恢复后，靶机会补传缓存的命中事件。重传流可能乱序、
重复甚至自相矛盾。本模块按发次序号建立规范顺序，只标注不修改：
原始事件一律保留，冲突发次交裁判复核，绝不自动改写命中。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .models import ShotEvent


class OrderFlag(Enum):
    OK = "ok"
    LATE_RETRANSMIT = "late_retransmit"  # 超出重传宽限的补传
    DUPLICATE = "duplicate"  # 同一事件标识重复上传且内容一致
    CONFLICT = "conflict"  # 同一事件标识内容矛盾，需人工复核
    OUT_OF_ORDER = "out_of_order"  # 到达顺序与发次序号不一致
    SEQUENCE_GAP = "sequence_gap"  # 序号缺失，可能有未送达事件


@dataclass(frozen=True)
class OrderedShot:
    """规范顺序中的一发：原始事件 + 标注。"""

    event: ShotEvent
    flags: tuple[OrderFlag, ...] = (OrderFlag.OK,)

    @property
    def needs_review(self) -> bool:
        return OrderFlag.CONFLICT in self.flags or OrderFlag.SEQUENCE_GAP in self.flags


@dataclass
class ReorderResult:
    target_id: str
    ordered: list[OrderedShot] = field(default_factory=list)
    discarded_duplicates: list[ShotEvent] = field(default_factory=list)
    gaps: list[int] = field(default_factory=list)

    @property
    def review_shots(self) -> list[OrderedShot]:
        return [s for s in self.ordered if s.needs_review]


def reorder(events: list[ShotEvent], target_id: str,
            retransmit_grace_s: Optional[float] = None) -> ReorderResult:
    """把同一靶位的到达事件整理为规范发次顺序。

    - 按 (shot_seq, device_time, received_at) 排序建立规范顺序；
    - 同一 event_id 内容一致的重复上传去重并标注；
    - 同一 event_id 内容矛盾的保留先到达者并标 CONFLICT，双方均留档；
    - 到达顺序与规范顺序不一致的标 OUT_OF_ORDER；
    - 接收时间晚于设备时间超过宽限的标 LATE_RETRANSMIT；
    - 序号缺失记入 gaps，缺失前一发标 SEQUENCE_GAP 提示复核。
    """
    result = ReorderResult(target_id=target_id)

    # 1) 按 event_id 归并：识别重复与冲突
    by_id: dict[str, list[ShotEvent]] = {}
    for ev in events:
        by_id.setdefault(ev.event_id, []).append(ev)

    survivors: list[tuple[ShotEvent, bool]] = []  # (事件, 是否冲突)
    for group in by_id.values():
        group.sort(key=lambda e: e.received_at)
        first = group[0]
        identical = all(
            ev.device_time == first.device_time
            and ev.score == first.score
            and ev.shot_seq == first.shot_seq
            and ev.athlete_id == first.athlete_id
            for ev in group[1:]
        )
        if len(group) > 1 and identical:
            result.discarded_duplicates.extend(group[1:])
            survivors.append((first, False))
        elif len(group) > 1:
            # 内容矛盾：保留先到达者，全部留档待复核，不改写任何一方
            survivors.append((first, True))
        else:
            survivors.append((first, False))

    # 2) 建立规范顺序
    survivors.sort(key=lambda pair: (pair[0].shot_seq, pair[0].device_time, pair[0].received_at))
    canonical = [ev for ev, _ in survivors]
    # 到达位次按去重后的首次到达计算，重复副本不干扰乱序判定
    first_arrivals = sorted(canonical, key=lambda e: e.received_at)
    arrival_rank = {id(ev): rank for rank, ev in enumerate(first_arrivals)}

    # 3) 序号缺口
    seqs = sorted(ev.shot_seq for ev in canonical)
    if seqs:
        expected = set(range(seqs[0], seqs[-1] + 1))
        result.gaps = sorted(expected - set(seqs))

    # 4) 逐发标注
    for (ev, conflict), canon_rank in zip(survivors, range(len(survivors))):
        flags: list[OrderFlag] = []
        if conflict:
            flags.append(OrderFlag.CONFLICT)
        if len(by_id[ev.event_id]) > 1 and not conflict:
            flags.append(OrderFlag.DUPLICATE)
        if arrival_rank[id(ev)] != canon_rank:
            flags.append(OrderFlag.OUT_OF_ORDER)
        if retransmit_grace_s is not None:
            lag = (ev.received_at - ev.device_time).total_seconds()
            if lag > retransmit_grace_s:
                flags.append(OrderFlag.LATE_RETRANSMIT)
        if result.gaps and any(g == ev.shot_seq + 1 for g in result.gaps):
            flags.append(OrderFlag.SEQUENCE_GAP)
        result.ordered.append(OrderedShot(
            event=ev,
            flags=tuple(flags) if flags else (OrderFlag.OK,),
        ))
    return result
