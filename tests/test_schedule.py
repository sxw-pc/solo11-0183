"""合成旱季跨牧户预约调度的跨模块测试。

全部事件在测试内构造（或来自 fixtures/schedule_synth_*.json 合成案例），
不借用原仓只含单一草区 P1 的 fixture。每种放行结果都沿
permit→movement→recovery 三入口复核许可、分区压力与恢复状态。
"""
import json
import tempfile
import unittest
from pathlib import Path

from rangeledger import schedule
from rangeledger.store import (connect, ingest, evidence, save_decisions,
                               report, report_history, log_rejection)

ROOT = Path(__file__).resolve().parents[1]
SUBJECT = 'dry-season-synth'
SURFACES = schedule


def ev(kind, eid, time, received, data, subject=SUBJECT):
    return dict(id=eid, kind=kind, subject=subject, event_time=time,
                received_at=received, data=data)


def base_events(subject=SUBJECT, locked=False, quota=10,
                grass_z2=0.8, include_z2_cap=True):
    """构造 A/B 双群、Z1/Z2 两区的完整合成证据。"""
    sig_a = ['holder', 'vet'] if locked else ['holder']
    locked_zone = 'Z1' if locked else None
    out = [
        ev('pasture', 'e-pasture', 1, 1,
           {'parcels': ['Z1', 'Z2'], 'corridor_exclusive': True}, subject),
        ev('permit', 'e-permit-A', 10, 10,
           {'herd': 'A', 'holder': 'HA', 'start': 1, 'end': 3, 'quota': quota,
            'zones': ['Z1', 'Z2'], 'signatures': sig_a,
            'locked_zone': locked_zone}, subject),
        ev('permit', 'e-permit-B', 11, 11,
           {'herd': 'B', 'holder': 'HB', 'start': 1, 'end': 3, 'quota': quota,
            'zones': ['Z1'], 'signatures': ['holder'], 'locked_zone': None},
           subject),
        ev('herd', 'e-herd-A', 20, 20,
           {'herd': 'A', 'holder': 'HA', 'count': 2, 'tags': ['TA1', 'TA2']},
           subject),
        ev('herd', 'e-herd-B', 21, 21,
           {'herd': 'B', 'holder': 'HB', 'count': 2, 'tags': ['TB1', 'TB2']},
           subject),
        ev('boundary', 'e-boundary-B', 22, 22,
           {'kind': 'herd_deny', 'herd': 'B', 'zone': 'Z2',
            'reason': '接种留观，乙群只可去Z1'}, subject),
        ev('boundary', 'e-boundary-corridor', 23, 23,
           {'kind': 'corridor_exclusive', 'zones': ['Z1', 'Z2']}, subject),
        ev('vegetation_sample', 'e-grass-Z1', 30, 30,
           {'zone': 'Z1', 'grass': 0.8}, subject),
        ev('vegetation_sample', 'e-grass-Z2', 31, 31,
           {'zone': 'Z2', 'grass': grass_z2}, subject),
        ev('zone_capacity', 'e-cap-Z1', 40, 40,
           {'zone': 'Z1', 'capacity': 4, 'unit': 'animal_hours'}, subject),
    ]
    if include_z2_cap:
        out.append(ev('zone_capacity', 'e-cap-Z2', 41, 41,
                      {'zone': 'Z2', 'capacity': 4, 'unit': 'animal_hours'},
                      subject))
    out += [
        ev('booking', 'e-book-A', 50, 50,
           {'action': 'book', 'herd': 'A', 'holder': 'HA', 'start': 1,
            'end': 3, 'count': 2, 'zones': ['Z1', 'Z2']}, subject),
        ev('booking', 'e-book-B', 51, 51,
           {'action': 'book', 'herd': 'B', 'holder': 'HB', 'start': 1,
            'end': 3, 'count': 2, 'zones': ['Z1']}, subject),
    ]
    return out


def apply_plan(db, subject=SUBJECT, alloc_version=1):
    """完整入口：保存调度决定、投影 movement、三入口复核、版本化落库。"""
    records = evidence(db, subject)
    with db:
        results = schedule.decide(records)
        save_decisions(db, subject, results)
        for m in schedule.projected_movements(records, alloc_version):
            ingest(db, m)
        records = evidence(db, subject)
        adj, triples = schedule.adjudicate(records, alloc_version)
        save_decisions(db, subject, triples)
        # 受理异常同时进入 rejection 证据表，保证可回放。
        intake = next((r for n, r, _ in schedule.decide(records)
                       if n == SURFACES.SURFACE_INTAKE), None)
        if intake:
            for item in intake['anomalies']:
                log_rejection(
                    db, f"rej-v{alloc_version}-{item['event']}",
                    item['kind'], item)
    return report(db, subject), adj


def surface(db, name, subject=SUBJECT):
    return report(db, subject)[name]['result']


class SchedulerContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'case.db')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def load(self, events):
        with self.db:
            for e in events:
                ingest(self.db, e)

    # ---- 契约一：贪心只能放一群，交换可放两群 -------------------------
    def test_greedy_fails_but_global_exchange_places_both(self):
        self.load(base_events())
        rep, adj = apply_plan(self.db)
        greedy = rep[SURFACES.SURFACE_GREEDY]['result']
        self.assertFalse(greedy['feasible'])
        self.assertEqual([p['herd'] for p in greedy['placements']], ['A'])
        self.assertEqual(greedy['placements'][0]['zone'], 'Z1')
        self.assertEqual([b['herd'] for b in greedy['blocked']], ['B'])

        glob = rep[SURFACES.SURFACE_GLOBAL]['result']
        self.assertTrue(glob['feasible'])
        table = rep[SURFACES.SURFACE_TABLE]['result']
        self.assertEqual(rep[SURFACES.SURFACE_PROTOCOL]['result']['selected'],
                         'exchangeable')
        placed = {(r['herd'], r['zone'], tuple(r['window']))
                  for r in table['admitted']}
        self.assertEqual(placed,
                         {('A', 'Z2', (1.0, 3.0)), ('B', 'Z1', (1.0, 3.0))})
        self.assertEqual(table['deferred'], [])
        # 每群 2 头 × 2 时段 = 4 动物小时，两区各 4。
        self.assertTrue(all(r['animal_hours'] == 4.0
                            for r in table['admitted']))

    # ---- 契约二：可交换签认下沿 permit→movement→recovery 核对 ----------
    def test_exchangeable_protocol_full_chain(self):
        self.load(base_events())
        rep, adj = apply_plan(self.db)
        # permit 入口：两份许可都在有效期内。
        self.assertEqual(adj['许可期限放行'], '准入')
        for row in rep[SURFACES.SURFACE_PERMIT]['result'].values():
            self.assertTrue(row['permit_valid'])
        # movement 入口：投影事件按区汇总为 {Z1:4, Z2:4}。
        self.assertEqual(adj['跨区占用分配'], {'Z1': 4.0, 'Z2': 4.0})
        self.assertEqual(adj['防疫隔离通行'], '允许')
        self.assertEqual(adj['夜间越界巡护'], 0)
        # recovery 入口：总占用 8，容量 8，开放且无补偿。
        self.assertEqual(adj['movement投影核对']['total_animal_hours'], 8.0)
        self.assertEqual(adj['植被恢复阈值判定'], '开放')
        self.assertEqual(adj['休牧令执行与重开'], '重开')
        self.assertEqual(adj['超载补偿分摊'], {'HA': 0.0, 'HB': 0.0})
        # 投影事件必须带版本、来源预约与来源许可，落库可回放。
        moves = [e for e in evidence(self.db, SUBJECT) if e['kind'] == 'movement']
        self.assertEqual(len(moves), 2)
        for m in moves:
            self.assertEqual(m['data']['alloc_version'], 1)
            self.assertIn(m['data']['source_booking'], ('e-book-A', 'e-book-B'))
            self.assertTrue(m['data']['source_permit'])

    # ---- 契约三：双签锁区不可转签，B 待改期且不得伪造 Z2 许可 ----------
    def test_locked_protocol_defers_B_without_forging_Z2(self):
        self.load(base_events(locked=True))
        rep, adj = apply_plan(self.db)
        self.assertEqual(rep[SURFACES.SURFACE_PROTOCOL]['result']['selected'],
                         'locked')
        table = rep[SURFACES.SURFACE_TABLE]['result']
        self.assertEqual([(r['herd'], r['zone']) for r in table['admitted']],
                         [('A', 'Z1')])
        self.assertEqual(len(table['deferred']), 1)
        self.assertEqual(table['deferred'][0]['herd'], 'B')
        self.assertEqual(table['deferred'][0]['status'], '待改期')
        blocked = rep[SURFACES.SURFACE_BLOCKED]['result']
        self.assertEqual([b['herd'] for b in blocked], ['B'])
        # 被阻挡约束同时说明 Z1 被占与 Z2 的群级禁行边界，不伪造 B 的 Z2 许可。
        b_reasons = blocked[0]['reasons']
        self.assertIn('Z1', b_reasons)
        self.assertEqual(b_reasons['Z2'], '群级禁行边界（接种留观）')
        self.assertEqual(table['deferred'][0]['reasons']['Z2'],
                         '群级禁行边界（接种留观）')
        # 全局解同样不可行：锁区约束让交换无可行解。
        self.assertFalse(rep[SURFACES.SURFACE_GLOBAL]['result']['feasible'])
        # 只投影 A 的 movement，不存在 B 的 Z2 入场事实。
        zones = adj['跨区占用分配']
        self.assertEqual(zones, {'Z1': 4.0})
        self.assertNotIn('Z2', zones)
        moves = [(e['data']['herd'], e['data']['zone'])
                 for e in evidence(self.db, SUBJECT)
                 if e['kind'] == 'movement']
        self.assertEqual(moves, [('A', 'Z1')])
        # 许可仍判准入（A 的许可在有效期内），恢复只看到 HA 的 4 动物小时。
        self.assertEqual(adj['许可期限放行'], '准入')
        self.assertEqual(adj['movement投影核对']['holders'], {'HA': 4.0})
        self.assertEqual(adj['movement投影核对']['total_animal_hours'], 4.0)

    # ---- 恢复裁决：超载时按 holder 分摊补偿 ---------------------------
    def test_recovery_apportions_compensation_by_holder(self):
        # 配额 6 → 容量 = 6 × 0.8 = 4.8，总占用 8，超额 3.2。
        self.load(base_events(quota=6))
        rep, adj = apply_plan(self.db)
        self.assertEqual(adj['movement投影核对']['capacity'], 4.8)
        self.assertEqual(adj['植被恢复阈值判定'], '休牧')
        comp = adj['超载补偿分摊']
        # 两户各 4 动物小时，占比各半：3.2 × 0.5 × 10 = 16。
        self.assertEqual(comp, {'HA': 16.0, 'HB': 16.0})
        # 同一组申请在缺少 Z2 开放证据时，局部贪心/全局解都只能放 A 到 Z1，
        # 投影占用只有 {Z1:4}，与全局交换方案的 {Z1:4,Z2:4} 形成对照。
        from rangeledger.common import pressure
        greedy_events = base_events(include_z2_cap=False)
        g_proj = schedule.projected_movements(greedy_events, 99)
        _, g_zones = pressure(greedy_events + g_proj)
        self.assertEqual(g_zones, {'Z1': 4.0})
        g_table = next(r for n, r, _ in schedule.decide(greedy_events)
                       if n == SURFACES.SURFACE_TABLE)
        self.assertEqual([(x['herd'], x['zone']) for x in g_table['admitted']],
                         [('A', 'Z1')])
        self.assertEqual([x['herd'] for x in g_table['deferred']], ['B'])

    # ---- 休牧令与重检仍作用于投影后的恢复裁决 -------------------------
    def test_rest_order_blocks_reopening_for_projected_occupancy(self):
        events = base_events()
        events.append(ev('rest_order', 'e-rest', 60, 60,
                         {'active': True, 'deadline': 3}))
        self.load(events)
        rep, adj = apply_plan(self.db)
        self.assertEqual(adj['休牧令执行与重开'], '清场中')
        self.assertEqual(adj['植被恢复阈值判定'], '休牧')

    # ---- Z2 草量补录晚到：新版本重算，旧决定保持可回放 ----------------
    def test_late_z2_grass_recomputes_new_version_without_rewriting(self):
        # 第一版缺少 Z2 容量证据：A 只能进 Z1，B 无区可去而待改期。
        self.load(base_events(include_z2_cap=False))
        rep1, adj1 = apply_plan(self.db, alloc_version=1)
        v1_table = rep1[SURFACES.SURFACE_TABLE]['result']
        self.assertEqual([(r['herd'], r['zone']) for r in v1_table['admitted']],
                         [('A', 'Z1')])
        self.assertEqual([r['herd'] for r in v1_table['deferred']], ['B'])
        self.assertEqual(adj1['movement投影核对']['total_animal_hours'], 4.0)
        v1_moves = [(e['data']['herd'], e['data']['zone'])
                    for e in evidence(self.db, SUBJECT)
                    if e['kind'] == 'movement']
        self.assertEqual(v1_moves, [('A', 'Z1')])
        v1_history = report_history(self.db, SUBJECT, SURFACES.SURFACE_TABLE)
        self.assertEqual([h['version'] for h in v1_history], [1])

        # Z2 容量补录晚到，重算分配版本。
        with self.db:
            ingest(self.db, ev('zone_capacity', 'e-cap-Z2-late', 70, 70,
                               {'zone': 'Z2', 'capacity': 4,
                                'unit': 'animal_hours'}))
        rep2, adj2 = apply_plan(self.db, alloc_version=2)
        placed = {(r['herd'], r['zone'])
                  for r in rep2[SURFACES.SURFACE_TABLE]['result']['admitted']}
        self.assertEqual(placed, {('A', 'Z2'), ('B', 'Z1')})
        self.assertEqual(adj2['movement投影核对']['total_animal_hours'], 8.0)

        # 旧版本的“B 未分配”决定未被悄悄改写成已入场事实。
        history = report_history(self.db, SUBJECT, SURFACES.SURFACE_TABLE)
        self.assertEqual([h['version'] for h in history], [1, 2])
        self.assertEqual(
            [r['herd'] for r in history[0]['result']['deferred']], ['B'])
        self.assertEqual(len(history[1]['result']['admitted']), 2)
        # 两个版本的投影 movement 各自留痕，v1 中始终没有 B 的入场事件。
        v1_stored = [(e['data']['herd'], e['data']['zone'])
                     for e in evidence(self.db, SUBJECT)
                     if e['kind'] == 'movement'
                     and e['data']['alloc_version'] == 1]
        self.assertEqual(v1_stored, [('A', 'Z1')])
        versions = sorted({e['data']['alloc_version']
                           for e in evidence(self.db, SUBJECT)
                           if e['kind'] == 'movement'})
        self.assertEqual(versions, [1, 2])

    # ---- 重复预约去重并留 rejection 证据 ------------------------------
    def test_duplicate_booking_is_deduplicated_and_recorded(self):
        events = base_events()
        events.append(ev('booking', 'e-book-A-dup', 52, 52,
                         {'action': 'book', 'herd': 'A', 'holder': 'HA',
                          'start': 1, 'end': 3, 'count': 2,
                          'zones': ['Z1', 'Z2']}))
        self.load(events)
        rep, _ = apply_plan(self.db)
        intake = rep[SURFACES.SURFACE_INTAKE]['result']
        kinds = [a['kind'] for a in intake['anomalies']]
        self.assertIn('重复预约', kinds)
        dup = next(x for x in intake['ledger'] if x['id'] == 'e-book-A-dup')
        self.assertEqual(dup['status'], 'duplicate')
        self.assertEqual(dup['duplicate_of'], 'e-book-A')
        rej = self.db.execute('SELECT * FROM rejection').fetchall()
        self.assertTrue(any(r['reason'] == '重复预约' for r in rej))
        # 分配表仍只含 A/B 各一行。
        self.assertEqual(
            len(rep[SURFACES.SURFACE_TABLE]['result']['admitted']), 2)

    # ---- 时区折返：event_time 回跳但按 received_at 回放 ---------------
    def test_timezone_foldback_uses_received_order(self):
        events = base_events()
        # event_time 早于 A，但 received_at 更晚；受理次序仍为 A 后该单。
        events.append(ev('booking', 'e-book-C-fold', 5, 53,
                         {'action': 'book', 'herd': 'C', 'holder': 'HC',
                          'start': 1, 'end': 3, 'count': 2,
                          'zones': ['Z1', 'Z2']}))
        self.load(events)
        rep, _ = apply_plan(self.db)
        ledger = rep[SURFACES.SURFACE_INTAKE]['result']['ledger']
        order = [x['id'] for x in ledger if x['status'] == 'booked']
        self.assertEqual(order.index('e-book-A') < order.index('e-book-C-fold'),
                         True)

    # ---- 同群跨区冲突留存 ---------------------------------------------
    def test_same_herd_cross_zone_conflict_is_retained(self):
        events = base_events()
        events.append(ev('booking', 'e-book-A-cross', 53, 53,
                         {'action': 'book', 'herd': 'A', 'holder': 'HA',
                          'start': 2, 'end': 4, 'count': 2,
                          'zones': ['Z1']}))
        self.load(events)
        rep, _ = apply_plan(self.db)
        intake = rep[SURFACES.SURFACE_INTAKE]['result']
        self.assertIn('同群跨区冲突', [a['kind'] for a in intake['anomalies']])
        cross = next(x for x in intake['ledger']
                     if x['id'] == 'e-book-A-cross')
        self.assertEqual(cross['status'], 'cross_zone_conflict')
        self.assertEqual(cross['conflict_with'], 'e-book-A')

    # ---- 取消先于申请到达：孤儿取消留痕，不抛异常 ---------------------
    def test_cancel_arriving_before_booking_is_replayed(self):
        events = base_events()
        events.append(ev('booking', 'e-book-cancel-early', 49, 49,
                         {'action': 'cancel', 'herd': 'A', 'holder': 'HA',
                          'start': 1, 'end': 3, 'count': 2,
                          'zones': ['Z1', 'Z2']}))
        self.load(events)
        rep, _ = apply_plan(self.db)
        intake = rep[SURFACES.SURFACE_INTAKE]['result']
        self.assertIn('取消先于申请到达',
                      [a['kind'] for a in intake['anomalies']])
        row = next(x for x in intake['ledger']
                   if x['id'] == 'e-book-cancel-early')
        self.assertEqual(row['status'], 'orphan_cancel')

    # ---- 已入场后改期：原入场事实保留为被取代版本 ---------------------
    def test_reschedule_after_admission_keeps_historical_version(self):
        self.load(base_events())
        rep1, adj1 = apply_plan(self.db, alloc_version=1)
        self.assertEqual(adj1['movement投影核对']['total_animal_hours'], 8.0)
        with self.db:
            ingest(self.db, ev('booking', 'e-book-A-resched', 80, 80,
                               {'action': 'reschedule', 'reschedule_of': 'e-book-A',
                                'herd': 'A', 'holder': 'HA', 'start': 4,
                                'end': 6, 'count': 2,
                                'zones': ['Z1', 'Z2']}))
        rep2, adj2 = apply_plan(self.db, alloc_version=2)
        ledger = rep2[SURFACES.SURFACE_INTAKE]['result']['ledger']
        old = next(x for x in ledger if x['id'] == 'e-book-A')
        self.assertEqual(old['status'], 'superseded_by_reschedule')
        new = next(x for x in ledger if x['id'] == 'e-book-A-resched')
        self.assertEqual(new['status'], 'rescheduled')
        # v1 的入场 movement 仍然存在，没有被删除或改写。
        v1_moves = [e for e in evidence(self.db, SUBJECT)
                    if e['kind'] == 'movement'
                    and e['data']['alloc_version'] == 1]
        self.assertEqual(len(v1_moves), 2)
        history = report_history(self.db, SUBJECT, SURFACES.SURFACE_TABLE)
        self.assertEqual([h['version'] for h in history], [1, 2])

    # ---- 合成 fixture 可独立运行且明确不是原仓 fixture ----------------
    def test_synthetic_fixtures_run_through_cli_entry(self):
        from rangeledger.__main__ import run_fixture
        for path in (ROOT / 'fixtures' / 'schedule_synth_exchange.json',
                     ROOT / 'fixtures' / 'schedule_synth_locked.json'):
            payload = json.loads(path.read_text(encoding='utf8'))
            self.assertIn('synthetic', payload['case'])
            subject = payload['events'][0]['subject']
            out = run_fixture(self.db, path, set(schedule.KINDS))
            self.assertIn(subject, out)
        # 原仓 fixture 只有 P1，合成案例独立使用 Z1/Z2，互不引用。
        original = json.loads((ROOT / 'fixtures' / 'move_ok.json').read_text())
        zones = {d.get('parcel') for e in original['events']
                 for d in [e['data']]}
        self.assertEqual(zones, {'P1'})

    # ---- 幂等：重复执行入口不产生重复 movement 或重复决定版本 ---------
    def test_apply_plan_is_idempotent(self):
        self.load(base_events())
        apply_plan(self.db, alloc_version=1)
        moves_before = [e['id'] for e in evidence(self.db, SUBJECT)
                        if e['kind'] == 'movement']
        history_before = report_history(self.db, SUBJECT,
                                        SURFACES.SURFACE_TABLE)
        apply_plan(self.db, alloc_version=1)
        moves_after = [e['id'] for e in evidence(self.db, SUBJECT)
                       if e['kind'] == 'movement']
        history_after = report_history(self.db, SUBJECT,
                                       SURFACES.SURFACE_TABLE)
        self.assertEqual(sorted(moves_before), sorted(moves_after))
        self.assertEqual(history_before, history_after)
        rej_before = self.db.execute('SELECT COUNT(*) c FROM rejection').fetchone()['c']
        apply_plan(self.db, alloc_version=1)
        rej_after = self.db.execute('SELECT COUNT(*) c FROM rejection').fetchone()['c']
        self.assertEqual(rej_before, rej_after)

    # ---- 许可失效：预约窗口超出许可有效期，整群不得分配 ---------------
    def test_expired_permit_blocks_placement(self):
        events = base_events()
        for e in events:
            if e['id'] == 'e-permit-B':
                e['data']['end'] = 2  # B 预约 1–3 超出许可 1–2
        self.load(events)
        rep, adj = apply_plan(self.db)
        check = rep[SURFACES.SURFACE_PERMIT]['result']['e-book-B']
        self.assertFalse(check['permit_valid'])
        table = rep[SURFACES.SURFACE_TABLE]['result']
        self.assertEqual([r['herd'] for r in table['admitted']], ['A'])
        self.assertEqual(table['deferred'][0]['herd'], 'B')
        self.assertIn('许可缺失或不在预约有效期内',
                      table['deferred'][0]['reasons'].values())
        # 不产生 B 的入场 movement。
        self.assertNotIn('HB', adj['movement投影核对']['holders'])

    # ---- 数值容量：容量不足 4 动物小时时该区不可派 --------------------
    def test_numeric_zone_capacity_shortfall_blocks(self):
        events = base_events()
        for e in events:
            if e['id'] == 'e-cap-Z1':
                e['data']['capacity'] = 3  # 每群需求 4，Z1 放不下任何一群
        self.load(events)
        rep, _ = apply_plan(self.db)
        table = rep[SURFACES.SURFACE_TABLE]['result']
        # A 改去容量充足的 Z2；B 唯一可选的 Z1 容量不足，待改期。
        self.assertEqual([(r['herd'], r['zone']) for r in table['admitted']],
                         [('A', 'Z2')])
        self.assertEqual(table['deferred'][0]['herd'], 'B')
        self.assertEqual(table['deferred'][0]['reasons']['Z1'],
                         '分区动物小时容量不足')

    # ---- 决定均带证据 ID，可被 replay 复核 ----------------------------
    def test_every_decision_carries_evidence_ids(self):
        self.load(base_events())
        apply_plan(self.db)
        for name, item in report(self.db, SUBJECT).items():
            self.assertTrue(item['evidence_ids'], name)


if __name__ == '__main__':
    unittest.main()
