"""赛前-赛中-赛后全流程演示：巡检、漂移告警、重传排序、影响范围报告。

运行：python demo.py
"""

from pathlib import Path

from src.drift import DriftMonitor
from src.inspection import during_match, post_match, pre_match
from src.models import fmt, utc
from src.reorder import reorder
from src.report import assess_target
from src.rules import Rules
from src.scenario import load_scenario

BASE = Path(__file__).parent


def show_checks(title, report):
    print(f"\n== {title} ==")
    for r in report.results:
        mark = "通过" if r.passed else ("阻断" if r.blocking else "关注")
        print(f"  [{mark}] {r.target_id} {r.check_id}: {r.detail}")
    return report


def main():
    rules = Rules.load(BASE / "fixtures" / "rules.json")
    sc = load_scenario(BASE / "fixtures" / "scenario.json")
    monitor = DriftMonitor(rules)
    print(f"赛事：{sc.match['event']}（{sc.match['started_at']} ~ {sc.match['ended_at']}）")
    print(f"规则：偏差告警 ±{rules.warn_offset_ms:.0f}ms，隔离 ±{rules.isolate_offset_ms:.0f}ms，"
          f"漂移率告警 ±{rules.warn_drift_ppm:.0f}ppm")

    # 赛前巡检：固件/校准源登记 + 初始同步偏差
    fw = {t: r.version for t, r in sc.firmware.items()}
    pre_samples = [s for s in sc.sync_samples if s.sampled_at < utc(sc.match["started_at"])]
    for s in pre_samples:
        monitor.observe(s)
    pre = pre_match(monitor, rules, sc.target_ids, fw, sc.target_sources,
                    utc(sc.match["started_at"]))
    show_checks("赛前巡检（放行闸门）", pre)
    print(f"  放行靶位：{', '.join(sorted(pre.targets_cleared()))}")

    # 赛中：按时间轴交错处理同步采样、人工复位与技术官员恢复
    print("\n== 赛中实时监测 ==")
    live = [s for s in sc.sync_samples if s.sampled_at >= utc(sc.match["started_at"])]
    timeline = [(s.sampled_at, "sample", s) for s in live]
    timeline += [(r.reset_at, "reset", r) for r in sc.resets]
    timeline += [(utc(ri["at"]), "reinstate", ri) for ri in sc.reinstatements]
    timeline.sort(key=lambda item: item[0])
    for at, kind, payload in timeline:
        if kind == "reset":
            monitor.record_reset(payload)
            print(f"  {fmt(at)} 人工复位登记：{payload.target_id} "
                  f"操作员 {payload.operator}——{payload.reason}")
            continue
        if kind == "reinstate":
            rec = monitor.reinstate(payload["target_id"], payload["official_id"],
                                    at, payload["note"])
            print(f"  {fmt(at)} 技术官员 {rec.reinstated_by} 确认恢复 "
                  f"{rec.target_id}：{payload['note']}")
            continue
        verdict = monitor.observe(payload)
        for a in verdict.alerts:
            print(f"  {fmt(a.raised_at)} [{a.level.value}] {a.target_id} "
                  f"{a.metric}={a.value:+.1f}（阈值 {a.threshold:.0f}）{a.detail}")
        state = monitor.state(payload.target_id)
        if state.quarantined and state.quarantines[-1].opened_at == payload.sampled_at:
            print(f"  {fmt(payload.sampled_at)} >>> {payload.target_id} 已自动隔离："
                  f"{state.quarantines[-1].reason}")

    mid = during_match(monitor, rules, sc.target_ids, utc(sc.match["ended_at"]))
    show_checks("赛中巡检", mid)

    # 离线重传排序
    print("\n== 离线重传排序 ==")
    reordered = {}
    for tid in sc.target_ids:
        events = [s for s in sc.shots if s.target_id == tid]
        res = reorder(events, tid, retransmit_grace_s=rules.retransmit_grace_s)
        reordered[tid] = res
        print(f"  {tid}：规范顺序 {[s.event.shot_seq for s in res.ordered]}"
              f"（去重 {len(res.discarded_duplicates)} 条，缺口 {res.gaps or '无'}）")
        for s in res.ordered:
            flags = ",".join(f.value for f in s.flags)
            if flags != "ok":
                print(f"    第{s.event.shot_seq}发 {s.event.event_id}: {flags}")

    # 赛后巡检
    post = post_match(monitor, rules, sc.target_ids, utc(sc.match["ended_at"]))
    show_checks("赛后巡检", post)

    # 影响范围报告
    print("\n== 影响范围报告 ==")
    for tid in sc.target_ids:
        outages = [o for o in sc.outages if o.target_id == tid]
        impact = assess_target(monitor, rules, reordered[tid], outages)
        print(f"\n  靶位 {tid}")
        for a in impact.assessments:
            ev = a.shot.event
            off = (f"{a.offset_at_shot_ms:+.1f}ms"
                   if a.offset_at_shot_ms is not None else "未知")
            print(f"    第{a.shot_seq:>2}发 {ev.athlete_id} 环值{ev.score:>4} "
                  f"设备时间 {fmt(ev.device_time)} 接收 {fmt(ev.received_at)} "
                  f"估计偏差 {off} -> {a.basis.value}")
            for reason in a.reasons:
                print(f"         · {reason}")
        for line in impact.certify_lines():
            print(f"  【认证】{line}")


if __name__ == "__main__":
    main()
