"""实时漂移监测与靶位隔离。

每个靶位维护一条同步采样序列：逐次计算偏差与漂移率，
越界即产生告警；达到隔离阈值自动隔离靶位。
恢复使用不自动发生——必须由技术官员确认后登记恢复。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .models import (
    Alert,
    AlertLevel,
    ManualReset,
    QuarantineRecord,
    QuarantineStatus,
    SyncSample,
)
from .rules import Rules


@dataclass(frozen=True)
class SampleVerdict:
    """单次同步采样的判定结果。"""

    sample: SyncSample
    offset_ms: float
    drift_ppm: Optional[float]  # 相对上一有效样本的漂移率；无基线时为 None
    alerts: tuple[Alert, ...]


@dataclass
class TargetClockState:
    """单个靶位的时钟监测状态。"""

    target_id: str
    samples: list[SyncSample] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    quarantines: list[QuarantineRecord] = field(default_factory=list)
    resets: list[ManualReset] = field(default_factory=list)

    @property
    def quarantined(self) -> bool:
        return bool(self.quarantines) and self.quarantines[-1].status is QuarantineStatus.OPEN

    def quarantine_at(self, moment: datetime) -> Optional[QuarantineRecord]:
        """返回覆盖该时刻的隔离记录，无则 None。"""
        for record in self.quarantines:
            if record.covers(moment):
                return record
        return None


class DriftMonitor:
    """按规则阈值评估同步采样，自动隔离越界靶位。"""

    def __init__(self, rules: Rules):
        self.rules = rules
        self._states: dict[str, TargetClockState] = {}

    def state(self, target_id: str) -> TargetClockState:
        return self._states.setdefault(target_id, TargetClockState(target_id))

    def record_reset(self, reset: ManualReset) -> None:
        """登记人工复位；复位后漂移基线重立，历史样本保留供追溯。"""
        self.state(reset.target_id).resets.append(reset)

    def observe(self, sample: SyncSample) -> SampleVerdict:
        """评估一次同步采样，必要时自动隔离靶位。"""
        state = self.state(sample.target_id)
        offset = sample.offset_ms
        drift = self._drift_ppm(state, sample)
        alerts: list[Alert] = []

        abs_offset = abs(offset)
        if abs_offset >= self.rules.isolate_offset_ms:
            alerts.append(self._alert(sample, AlertLevel.ISOLATE, "offset_ms", offset,
                                      self.rules.isolate_offset_ms, "偏差越隔离阈值"))
        elif abs_offset >= self.rules.warn_offset_ms:
            alerts.append(self._alert(sample, AlertLevel.WARN, "offset_ms", offset,
                                      self.rules.warn_offset_ms, "偏差越告警阈值"))

        if drift is not None:
            abs_drift = abs(drift)
            if abs_drift >= self.rules.isolate_drift_ppm:
                alerts.append(self._alert(sample, AlertLevel.ISOLATE, "drift_ppm", drift,
                                          self.rules.isolate_drift_ppm, "漂移率越隔离阈值"))
            elif abs_drift >= self.rules.warn_drift_ppm:
                alerts.append(self._alert(sample, AlertLevel.WARN, "drift_ppm", drift,
                                          self.rules.warn_drift_ppm, "漂移率越告警阈值"))

        state.samples.append(sample)
        state.alerts.extend(alerts)

        if any(a.level is AlertLevel.ISOLATE for a in alerts) and not state.quarantined:
            reasons = "；".join(a.detail for a in alerts if a.level is AlertLevel.ISOLATE)
            state.quarantines.append(QuarantineRecord(
                target_id=sample.target_id,
                opened_at=sample.sampled_at,
                reason=f"自动隔离：{reasons}",
            ))
        return SampleVerdict(sample, offset, drift, tuple(alerts))

    def reinstate(self, target_id: str, official_id: str, at: datetime,
                  note: str = "") -> QuarantineRecord:
        """技术官员确认后恢复靶位使用。无确认不得恢复。"""
        if not official_id or not official_id.strip():
            raise ValueError("恢复使用必须由技术官员确认，official_id 不能为空")
        state = self.state(target_id)
        if not state.quarantined:
            raise ValueError(f"靶位 {target_id} 当前未处于隔离状态")
        record = state.quarantines[-1]
        restored = QuarantineRecord(
            target_id=record.target_id,
            opened_at=record.opened_at,
            reason=record.reason,
            status=QuarantineStatus.REINSTATED,
            reinstated_at=at,
            reinstated_by=official_id.strip(),
            reinstate_note=note or None,
        )
        state.quarantines[-1] = restored
        return restored

    def _drift_ppm(self, state: TargetClockState, sample: SyncSample) -> Optional[float]:
        """相对上一有效样本的漂移率（ppm）。人工复位前的样本不作基线。"""
        last_reset = max(
            (r.reset_at for r in state.resets if r.reset_at <= sample.sampled_at),
            default=None,
        )
        baseline = None
        for prev in reversed(state.samples):
            if last_reset is not None and prev.sampled_at <= last_reset:
                break  # 复位点之前的样本不可作基线
            baseline = prev
            break
        if baseline is None:
            return None
        dt_s = (sample.reference_time - baseline.reference_time).total_seconds()
        if dt_s <= 0:
            return None
        # 偏差变化量 / 间隔：ms 偏差每秒 = 1000 ppm
        return (sample.offset_ms - baseline.offset_ms) / dt_s * 1000.0

    @staticmethod
    def _alert(sample: SyncSample, level: AlertLevel, metric: str,
               value: float, threshold: float, detail: str) -> Alert:
        return Alert(level=level, target_id=sample.target_id, metric=metric,
                     value=value, threshold=threshold,
                     raised_at=sample.sampled_at, detail=detail)
