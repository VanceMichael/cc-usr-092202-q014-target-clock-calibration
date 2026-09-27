"""靶位隔离台账：只追加事件，系统可自动隔离，恢复必须技术官员确认。

台账只记录隔离与恢复事实，不提供任何修改成绩事件的能力。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# 隔离原因
REASON_OFFSET_CRITICAL = "OFFSET_CRITICAL"        # 同步偏差越界
REASON_MANUAL_RESET = "MANUAL_RESET"              # 人工复位后未确认
REASON_INSPECTION_FAILED = "INSPECTION_FAILED"    # 赛前巡检不合格

REASON_LABELS = {
    REASON_OFFSET_CRITICAL: "同步偏差越出严重限值",
    REASON_MANUAL_RESET: "人工复位后未经技术官员确认",
    REASON_INSPECTION_FAILED: "赛前巡检不合格",
}


@dataclass(frozen=True)
class QuarantineEvent:
    lane: str
    reason: str
    quarantined_at: datetime
    detail: str
    recovered_at: datetime | None = None
    recovered_by: str | None = None
    recovery_note: str | None = None

    def active_at(self, moment: datetime) -> bool:
        if moment < self.quarantined_at:
            return False
        if self.recovered_at is None:
            return True
        return moment < self.recovered_at


class QuarantineLedger:
    """按时间顺序追加的隔离台账，查询任意时刻的隔离状态。"""

    def __init__(self) -> None:
        self._events: list[QuarantineEvent] = []

    @property
    def events(self) -> tuple[QuarantineEvent, ...]:
        return tuple(self._events)

    def is_quarantined(self, lane: str, moment: datetime) -> bool:
        return any(event.active_at(moment) for event in self._events if event.lane == lane)

    def active_quarantine(self, lane: str, moment: datetime) -> QuarantineEvent | None:
        for event in self._events:
            if event.lane == lane and event.active_at(moment):
                return event
        return None

    def quarantine(self, lane: str, reason: str, at: datetime, detail: str = "") -> QuarantineEvent | None:
        """系统自动隔离。已在隔离中则不重复登记，保持台账精简。"""
        if reason not in REASON_LABELS:
            raise ValueError(f"未知隔离原因：{reason}")
        if self.is_quarantined(lane, at):
            return None
        event = QuarantineEvent(
            lane=lane,
            reason=reason,
            quarantined_at=at,
            detail=detail or REASON_LABELS[reason],
        )
        self._events.append(event)
        return event

    def reinstate(self, lane: str, officer: str, at: datetime, note: str = "") -> QuarantineEvent:
        """技术官员确认恢复。缺少官员或未处于隔离状态都拒绝。"""
        if not officer or not officer.strip():
            raise PermissionError("恢复使用必须由技术官员确认，系统不得自行恢复")
        active = self.active_quarantine(lane, at)
        if active is None:
            raise ValueError(f"靶位 {lane} 当前不在隔离中，无需恢复")
        recovered = QuarantineEvent(
            lane=active.lane,
            reason=active.reason,
            quarantined_at=active.quarantined_at,
            detail=active.detail,
            recovered_at=at,
            recovered_by=officer.strip(),
            recovery_note=note,
        )
        self._events[self._events.index(active)] = recovered
        return recovered

    def open_quarantines(self, moment: datetime) -> tuple[QuarantineEvent, ...]:
        return tuple(event for event in self._events if event.active_at(moment))

    def lanes(self) -> tuple[str, ...]:
        return tuple(sorted({event.lane for event in self._events}))
