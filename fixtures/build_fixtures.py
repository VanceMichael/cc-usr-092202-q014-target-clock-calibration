"""生成 fixtures/ 下的公开样例数据（确定性，可重复执行）。

场景：女子十米气步枪资格赛（公开样例，不含真实个人信息）。
- A-01：时钟健康，60 发成绩申报新亚洲纪录（样例成绩 630.6）。
- A-02：赛中漂移越界自动隔离，随后网络中断、离线缓存乱序重传并出现重复报文，
  技术官员重新校准后确认恢复。
- A-03：人工复位后未及时重新同步，被隔离，重新同步后经确认恢复。
- A-04：赛前巡检不合格（未登记校准、固件未批准、时钟跳变），禁止投入使用。
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = datetime(2026, 9, 27, 9, 0, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parent.parent / "fixtures"


def ts(minutes: float, seconds: float = 0.0) -> str:
    moment = BASE + timedelta(minutes=minutes, seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def sample(lane: str, seq: int, minutes: float, offset_ms: float,
           received_minutes: float | None = None) -> dict:
    return {
        "lane": lane,
        "seq": seq,
        "master_time": ts(minutes),
        "offset_ms": offset_ms,
        "source": "GPS-PPS-01",
        "received_time": ts(received_minutes if received_minutes is not None else minutes),
    }


def shot(lane: str, seq: int, dev_minutes: float, dev_seconds: float,
         recv_minutes: float, recv_seconds: float, score: float) -> dict:
    inner_ten = score >= 10.8
    return {
        "lane": lane,
        "shot_seq": seq,
        "device_time": ts(dev_minutes, dev_seconds),
        "received_time": ts(recv_minutes, recv_seconds),
        "score": score,
        "inner_ten": inner_ten,
    }


def build_rules() -> dict:
    return {
        "match": {
            "id": "sample-w10arq-2026",
            "name": "女子十米气步枪资格赛（公开样例）",
            "start": ts(0),
            "end": ts(75),
            "inspection_time": ts(-5),
        },
        "thresholds": {
            "warn_offset_ms": 100.0,
            "critical_offset_ms": 250.0,
            "warn_drift_ppm": 50.0,
            "critical_drift_ppm": 200.0,
            "max_resync_gap_s": 300.0,
            "max_clock_jump_s": 5.0,
        },
        "approved_firmware": ["EST-5.2.1", "EST-5.3.0"],
        "calibration_sources": ["GPS-PPS-01", "NTP-POOL-A"],
        "inspection_checks": [
            {"id": "firmware_approved", "label": "固件版本在批准清单内"},
            {"id": "calibration_registered", "label": "校准源与校准时间已登记"},
            {"id": "baseline_offset", "label": "赛前基准偏差在限值内"},
            {"id": "clock_monotonic", "label": "设备时钟连续无跳变"},
            {"id": "no_open_incidents", "label": "无未闭环的网络中断或人工复位"},
        ],
    }


def build_targets() -> dict:
    return {
        "targets": [
            {
                "lane": "A-01",
                "device_id": "EST-A01",
                "firmware": "EST-5.3.0",
                "registered_at": ts(-45),
                "calibration_source": "GPS-PPS-01",
                "calibrated_at": ts(-40),
                "baseline_offset_ms": 12.0,
                "clock_jump_detected": False,
                "open_incident": False,
            },
            {
                "lane": "A-02",
                "device_id": "EST-A02",
                "firmware": "EST-5.3.0",
                "registered_at": ts(-45),
                "calibration_source": "GPS-PPS-01",
                "calibrated_at": ts(-40),
                "baseline_offset_ms": 15.0,
                "clock_jump_detected": False,
                "open_incident": False,
            },
            {
                "lane": "A-03",
                "device_id": "EST-A03",
                "firmware": "EST-5.3.0",
                "registered_at": ts(-45),
                "calibration_source": "GPS-PPS-01",
                "calibrated_at": ts(-40),
                "baseline_offset_ms": 20.0,
                "clock_jump_detected": False,
                "open_incident": False,
            },
            {
                "lane": "A-04",
                "device_id": "EST-A04",
                "firmware": "EST-4.9.0",
                "registered_at": ts(-45),
                "calibration_source": None,
                "calibrated_at": None,
                "baseline_offset_ms": None,
                "clock_jump_detected": True,
                "open_incident": True,
            },
        ]
    }


def build_telemetry() -> dict:
    sync_samples = [
        # A-01：始终在限值内
        sample("A-01", 1, 0, 12.0),
        sample("A-01", 2, 15, 18.0),
        sample("A-01", 3, 30, 21.0),
        sample("A-01", 4, 45, 25.0),
        sample("A-01", 5, 60, 28.0),
        sample("A-01", 6, 75, 30.0),
        # A-02：漂移逐步越界，中断期间样本缺失，恢复后补传（接收时间晚于主时钟时间）
        sample("A-02", 1, 0, 40.0),
        sample("A-02", 2, 15, 95.0),
        sample("A-02", 3, 25, 180.0),
        sample("A-02", 4, 35, 340.0),
        sample("A-02", 5, 55, 30.0, received_minutes=58.0),
        sample("A-02", 6, 60, 32.0),
        sample("A-02", 7, 75, 35.0),
        # A-03：复位前偏差正常，复位后样本缺失，重新同步后恢复
        sample("A-03", 1, 0, 22.0),
        sample("A-03", 2, 15, 30.0),
        sample("A-03", 3, 30, 35.0),
        sample("A-03", 4, 40, 8.0),
        sample("A-03", 5, 60, 12.0),
        sample("A-03", 6, 75, 15.0),
    ]
    incidents = [
        {
            "id": "INC-01",
            "lane": "A-02",
            "kind": "network_outage",
            "start": ts(38),
            "end": ts(52),
            "note": "靶位交换机端口故障，成绩本地缓存后补传",
        },
        {
            "id": "INC-02",
            "lane": "A-02",
            "kind": "manual_adjustment",
            "start": ts(53),
            "end": ts(55),
            "note": "技术官员现场重新校准并恢复同步",
        },
        {
            "id": "INC-03",
            "lane": "A-03",
            "kind": "manual_reset",
            "start": ts(33),
            "end": ts(33),
            "note": "设备维护后人工复位，未及时重新同步",
        },
    ]
    reinstatements = [
        {
            "lane": "A-02",
            "officer": "技术官员-丁",
            "confirmed_at": ts(56),
            "note": "重新校准后偏差回到限值内，确认恢复使用",
        },
        {
            "lane": "A-03",
            "officer": "技术官员-丁",
            "confirmed_at": ts(41),
            "note": "重新同步后确认恢复使用",
        },
    ]
    score_events = build_score_events()
    return {
        "sync_samples": sync_samples,
        "incidents": incidents,
        "reinstatements": reinstatements,
        "score_events": score_events,
    }


def build_score_events() -> list[dict]:
    events: list[dict] = []

    # A-01：60 发，设备时间按当时偏差推算，节奏均匀
    scores = [10.0, 10.7, 10.5, 10.8, 10.4, 10.6, 10.3, 10.9, 10.2, 10.7]
    offsets = [(0, 12.0), (15, 18.0), (30, 21.0), (45, 25.0), (60, 28.0), (75, 30.0)]

    def offset_at(minute: float) -> float:
        for (m0, o0), (m1, o1) in zip(offsets, offsets[1:]):
            if m0 <= minute <= m1:
                return o0 + (o1 - o0) * (minute - m0) / (m1 - m0)
        return offsets[-1][1]

    for index in range(60):
        minute = 1.0 + index * 1.2
        dev_total = minute * 60.0 + offset_at(minute) / 1000.0
        events.append(shot(
            "A-01", index + 1,
            dev_total / 60.0, dev_total % 60.0,
            minute, 0.4,
            scores[index % len(scores)],
        ))

    # A-02：含离线缓存乱序补传与重复报文（接收顺序见列表顺序）
    events.append(shot("A-02", 12, 30, 30.200, 30, 30.6, 10.4))
    events.append(shot("A-02", 13, 34, 10.340, 34, 10.8, 10.6))
    # 中断期间本地缓存的 5 发，恢复后乱序补传
    events.append(shot("A-02", 15, 44, 20.300, 52, 5.0, 10.1))
    events.append(shot("A-02", 17, 49, 50.300, 52, 6.0, 10.7))
    events.append(shot("A-02", 14, 39, 40.300, 52, 7.0, 10.9))
    events.append(shot("A-02", 16, 47, 10.300, 52, 8.0, 10.3))
    # 重传的重复报文：与 14 发设备时间与环值一致，不得改写原始命中
    events.append(shot("A-02", 14, 39, 40.300, 52, 9.0, 10.9))
    # 恢复使用后的正常一发
    events.append(shot("A-02", 18, 58, 5.030, 58, 5.4, 10.5))

    # A-03：复位前、复位后未同步（待复核）、重新同步后各一发
    events.append(shot("A-03", 5, 32, 10.035, 32, 10.5, 10.6))
    events.append(shot("A-03", 6, 35, 20.000, 35, 20.5, 10.2))
    events.append(shot("A-03", 7, 45, 30.008, 45, 30.5, 10.8))

    return events


def build_claims() -> dict:
    return {
        "claims": [
            {
                "id": "CLAIM-W10ARQ-ASIA",
                "label": "女子十米气步枪资格赛亚洲纪录申报（公开样例）",
                "lane": "A-01",
                "athlete": "运动员-甲（样例）",
                "claimed_total": 630.6,
                "shot_count": 60,
            }
        ]
    }


def main() -> None:
    outputs = {
        "rules.json": build_rules(),
        "targets.json": build_targets(),
        "telemetry.json": build_telemetry(),
        "claims.json": build_claims(),
    }
    for name, payload in outputs.items():
        path = ROOT / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"已生成 {path}")


if __name__ == "__main__":
    main()
