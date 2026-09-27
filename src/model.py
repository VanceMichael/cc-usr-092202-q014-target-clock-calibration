"""电子靶时钟校验服务的领域模型。

所有成绩事件同时保留设备时间（device_time）与接收时间（received_time），
模型层不提供任何修改原始报文的入口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

_TS_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"

IncidentKind = Literal["network_outage", "manual_reset", "manual_adjustment"]


def parse_ts(value: str) -> datetime:
    """解析样例中的 UTC 时间字符串（毫秒精度，Z 结尾）。"""
    text = value.strip()
    if text.endswith("Z"):
        dt = datetime.strptime(text, _TS_FMT)
        return dt.replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fmt_ts(value: datetime) -> str:
    """统一格式化为毫秒精度 UTC 字符串。"""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _require(mapping: dict, key: str, scope: str) -> Any:
    if key not in mapping:
        raise ValueError(f"{scope}缺少字段：{key}")
    return mapping[key]


@dataclass(frozen=True)
class Thresholds:
    """规则文件中的时钟限值。"""

    warn_offset_ms: float
    critical_offset_ms: float
    warn_drift_ppm: float
    critical_drift_ppm: float
    max_resync_gap_s: float
    max_clock_jump_s: float

    @classmethod
    def from_dict(cls, data: dict) -> "Thresholds":
        return cls(
            warn_offset_ms=float(data["warn_offset_ms"]),
            critical_offset_ms=float(data["critical_offset_ms"]),
            warn_drift_ppm=float(data["warn_drift_ppm"]),
            critical_drift_ppm=float(data["critical_drift_ppm"]),
            max_resync_gap_s=float(data["max_resync_gap_s"]),
            max_clock_jump_s=float(data["max_clock_jump_s"]),
        )


@dataclass(frozen=True)
class RuleSet:
    match_id: str
    match_name: str
    start: datetime
    end: datetime
    inspection_time: datetime
    thresholds: Thresholds
    approved_firmware: frozenset[str]
    calibration_sources: frozenset[str]
    inspection_checks: tuple[dict, ...]

    @classmethod
    def from_dict(cls, data: dict) -> "RuleSet":
        match = _require(data, "match", "规则")
        raw = _require(data, "thresholds", "规则")
        return cls(
            match_id=str(match["id"]),
            match_name=str(match["name"]),
            start=parse_ts(match["start"]),
            end=parse_ts(match["end"]),
            inspection_time=parse_ts(match["inspection_time"]),
            thresholds=Thresholds.from_dict(raw),
            approved_firmware=frozenset(data["approved_firmware"]),
            calibration_sources=frozenset(data["calibration_sources"]),
            inspection_checks=tuple(data.get("inspection_checks", ())),
        )


@dataclass(frozen=True)
class Target:
    """靶机固件与校准登记。"""

    lane: str
    device_id: str
    firmware: str
    registered_at: datetime | None
    calibration_source: str | None
    calibrated_at: datetime | None
    baseline_offset_ms: float | None
    clock_jump_detected: bool
    open_incident: bool

    @classmethod
    def from_dict(cls, data: dict) -> "Target":
        registered = data.get("registered_at")
        calibrated = data.get("calibrated_at")
        return cls(
            lane=str(_require(data, "lane", "靶机登记")),
            device_id=str(data["device_id"]),
            firmware=str(data["firmware"]),
            registered_at=parse_ts(registered) if registered else None,
            calibration_source=data.get("calibration_source"),
            calibrated_at=parse_ts(calibrated) if calibrated else None,
            baseline_offset_ms=(
                float(data["baseline_offset_ms"])
                if data.get("baseline_offset_ms") is not None
                else None
            ),
            clock_jump_detected=bool(data.get("clock_jump_detected", False)),
            open_incident=bool(data.get("open_incident", False)),
        )


@dataclass(frozen=True)
class SyncSample:
    """一次同步采样：主时钟时间、设备相对偏差、接收时间。"""

    lane: str
    seq: int
    master_time: datetime
    offset_ms: float
    source: str
    received_time: datetime

    @classmethod
    def from_dict(cls, data: dict) -> "SyncSample":
        return cls(
            lane=str(data["lane"]),
            seq=int(data["seq"]),
            master_time=parse_ts(data["master_time"]),
            offset_ms=float(data["offset_ms"]),
            source=str(data["source"]),
            received_time=parse_ts(data["received_time"]),
        )


@dataclass(frozen=True)
class Incident:
    """网络中断、人工复位或人工调整登记。"""

    id: str
    lane: str
    kind: IncidentKind
    start: datetime
    end: datetime | None
    note: str

    @classmethod
    def from_dict(cls, data: dict) -> "Incident":
        kind = str(_require(data, "kind", "事件登记"))
        if kind not in ("network_outage", "manual_reset", "manual_adjustment"):
            raise ValueError(f"不支持的事件类型：{kind}")
        end = data.get("end")
        return cls(
            id=str(data["id"]),
            lane=str(data["lane"]),
            kind=kind,  # type: ignore[arg-type]
            start=parse_ts(data["start"]),
            end=parse_ts(end) if end else None,
            note=str(data.get("note", "")),
        )

    def is_open_at(self, moment: datetime) -> bool:
        if self.end is None:
            return self.start <= moment
        return self.start <= moment <= self.end


@dataclass(frozen=True)
class Reinstatement:
    """技术官员确认恢复使用（系统不得自行生成）。"""

    lane: str
    officer: str
    confirmed_at: datetime
    note: str

    @classmethod
    def from_dict(cls, data: dict) -> "Reinstatement":
        officer = str(_require(data, "officer", "恢复确认"))
        if not officer.strip():
            raise ValueError("恢复使用必须由技术官员确认")
        return cls(
            lane=str(data["lane"]),
            officer=officer,
            confirmed_at=parse_ts(data["confirmed_at"]),
            note=str(data.get("note", "")),
        )


@dataclass(frozen=True)
class ScoreEvent:
    """逐发命中原始报文，双时间戳只读保留。"""

    lane: str
    shot_seq: int
    device_time: datetime
    received_time: datetime
    score: float

    @classmethod
    def from_dict(cls, data: dict) -> "ScoreEvent":
        return cls(
            lane=str(data["lane"]),
            shot_seq=int(data["shot_seq"]),
            device_time=parse_ts(data["device_time"]),
            received_time=parse_ts(data["received_time"]),
            score=float(data["score"]),
        )

    @property
    def delay_s(self) -> float:
        return (self.received_time - self.device_time).total_seconds()


@dataclass(frozen=True)
class Claim:
    """正式成绩 / 纪录申报。"""

    id: str
    label: str
    lane: str
    athlete: str
    claimed_total: float
    shot_count: int

    @classmethod
    def from_dict(cls, data: dict) -> "Claim":
        return cls(
            id=str(data["id"]),
            label=str(data["label"]),
            lane=str(data["lane"]),
            athlete=str(data["athlete"]),
            claimed_total=float(data["claimed_total"]),
            shot_count=int(data["shot_count"]),
        )
