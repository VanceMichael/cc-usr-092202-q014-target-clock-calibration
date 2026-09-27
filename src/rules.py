"""校验规则阈值，来自赛事技术规则配置（fixtures/rules.json）。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Rules:
    warn_offset_ms: float  # 偏差告警阈值
    isolate_offset_ms: float  # 偏差隔离阈值，越界自动隔离
    warn_drift_ppm: float  # 漂移率告警阈值
    isolate_drift_ppm: float  # 漂移率隔离阈值
    sync_interval_s: float  # 赛中同步采样要求间隔
    retransmit_grace_s: float  # 中断恢复后重传宽限
    final_sync_max_age_s: float  # 赛后末次同步采样最大龄期

    @classmethod
    def load(cls, path: Path) -> "Rules":
        data = json.loads(path.read_text(encoding="utf-8"))
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"规则文件含未知字段: {sorted(unknown)}")
        return cls(**data)
