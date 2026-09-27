"""读取设备与赛事场景样例（fixtures/scenario.json）。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .models import (
    CalibrationSource,
    FirmwareRecord,
    ManualReset,
    NetworkOutage,
    ShotEvent,
    SyncSample,
    utc,
)


@dataclass(frozen=True)
class Scenario:
    match: dict
    firmware: dict[str, FirmwareRecord]
    sources: dict[str, CalibrationSource]
    target_sources: dict[str, str]
    sync_samples: list[SyncSample]
    shots: list[ShotEvent]
    outages: list[NetworkOutage]
    resets: list[ManualReset]
    reinstatements: list[dict]

    @property
    def target_ids(self) -> list[str]:
        return sorted(self.firmware)


def load_scenario(path: Path) -> Scenario:
    raw = json.loads(path.read_text(encoding="utf-8"))
    firmware = {
        f["target_id"]: FirmwareRecord(
            target_id=f["target_id"], version=f["version"],
            checksum=f["checksum"], registered_at=utc(f["registered_at"]),
        )
        for f in raw["firmware"]
    }
    sources = {
        s["source_id"]: CalibrationSource(
            source_id=s["source_id"], kind=s["kind"],
            accuracy_ms=s["accuracy_ms"], registered_at=utc(s["registered_at"]),
        )
        for s in raw["sources"]
    }
    return Scenario(
        match=raw["match"],
        firmware=firmware,
        sources=sources,
        target_sources=raw["target_sources"],
        sync_samples=[
            SyncSample(
                target_id=s["target_id"], sampled_at=utc(s["sampled_at"]),
                device_time=utc(s["device_time"]),
                reference_time=utc(s["reference_time"]), source_id=s["source_id"],
            )
            for s in raw["sync_samples"]
        ],
        shots=[
            ShotEvent(
                event_id=s["event_id"], target_id=s["target_id"],
                athlete_id=s["athlete_id"], relay=s["relay"],
                shot_seq=s["shot_seq"], device_time=utc(s["device_time"]),
                received_at=utc(s["received_at"]), score=s["score"],
                payload_hash=s.get("payload_hash", ""),
            )
            for s in raw["shots"]
        ],
        outages=[
            NetworkOutage(
                target_id=o["target_id"], started_at=utc(o["started_at"]),
                recovered_at=utc(o["recovered_at"]) if o.get("recovered_at") else None,
            )
            for o in raw["outages"]
        ],
        resets=[
            ManualReset(
                target_id=r["target_id"], reset_at=utc(r["reset_at"]),
                operator=r["operator"], reason=r["reason"],
            )
            for r in raw["resets"]
        ],
        reinstatements=raw["reinstatements"],
    )
