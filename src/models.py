"""电子靶时钟校验领域模型。

所有时间统一为 UTC。每个成绩事件成对保留设备时间与接收时间；
原始命中一旦登记即不可改写，后续任何处置只追加标注与记录。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


def utc(iso: str) -> datetime:
    """解析 ISO 8601 时间并统一为 UTC。"""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt(dt: Optional[datetime]) -> str:
    """统一输出格式，便于报告阅读。"""
    if dt is None:
        return "-"
    return dt.astimezone(timezone.utc).strftime("%H:%M:%S")


class AlertLevel(Enum):
    WARN = "WARN"
    ISOLATE = "ISOLATE"


class QuarantineStatus(Enum):
    OPEN = "open"
    REINSTATED = "reinstated"


@dataclass(frozen=True)
class FirmwareRecord:
    """靶机固件登记。"""

    target_id: str
    version: str
    checksum: str
    registered_at: datetime


@dataclass(frozen=True)
class CalibrationSource:
    """校准源登记（GNSS/NTP/PTP/人工）。"""

    source_id: str
    kind: str
    accuracy_ms: float
    registered_at: datetime


@dataclass(frozen=True)
class SyncSample:
    """一次时钟同步采样：同一时刻的设备时间与基准时间。"""

    target_id: str
    sampled_at: datetime  # 采样到达服务的时刻（接收时间）
    device_time: datetime  # 设备自报时间
    reference_time: datetime  # 校准源基准时间
    source_id: str

    @property
    def offset_ms(self) -> float:
        """设备时间减基准时间，正值表示设备钟快。"""
        return (self.device_time - self.reference_time).total_seconds() * 1000.0


@dataclass(frozen=True)
class ShotEvent:
    """逐发命中事件。设备时间与接收时间成对保留，登记后不可改写。"""

    event_id: str
    target_id: str
    athlete_id: str
    relay: int
    shot_seq: int
    device_time: datetime
    received_at: datetime
    score: float
    payload_hash: str = ""


@dataclass(frozen=True)
class NetworkOutage:
    """网络中断区间；recovered_at 为 None 表示尚未恢复。"""

    target_id: str
    started_at: datetime
    recovered_at: Optional[datetime] = None


@dataclass(frozen=True)
class ManualReset:
    """人工复位登记。复位前后偏差不可比，漂移基线自此重立。"""

    target_id: str
    reset_at: datetime
    operator: str
    reason: str


@dataclass(frozen=True)
class Alert:
    level: AlertLevel
    target_id: str
    metric: str  # offset_ms / drift_ppm
    value: float
    threshold: float
    raised_at: datetime
    detail: str = ""


@dataclass(frozen=True)
class QuarantineRecord:
    """隔离记录。恢复使用必须登记技术官员标识与确认时间。"""

    target_id: str
    opened_at: datetime
    reason: str
    status: QuarantineStatus = QuarantineStatus.OPEN
    reinstated_at: Optional[datetime] = None
    reinstated_by: Optional[str] = None
    reinstate_note: Optional[str] = None

    def covers(self, moment: datetime) -> bool:
        """判断某时刻是否处于本隔离期内。"""
        if moment < self.opened_at:
            return False
        if self.status is QuarantineStatus.REINSTATED and self.reinstated_at is not None:
            return moment < self.reinstated_at
        return True
