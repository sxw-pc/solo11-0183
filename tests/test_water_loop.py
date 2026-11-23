import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from rangeledger.water_meter import (
    delta,
    sign_event,
    verify_event,
    meter_deltas,
    meter_volume,
    all_meter_volumes,
)
from rangeledger.water_ledger import (
    INFRASTRUCTURE,
    JOINT_STEWARDSHIP,
    build_pipes,
    reconcile,
    totals,
    naive_return_as_new_intake,
    naive_branch_fully_booked,
    return_path_present,
    impacted_nodes,
    issue_vouchers,
)
from rangeledger.store import (
    connect,
    ingest,
    evidence,
    save_node_balances,
    save_voucher,
    vouchers,
    report,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'water-loop.json'
SUBJECT = 'water-loop-1'
TOL = 1.0


def load_events():
    return deepcopy(json.loads(FIXTURE.read_text(encoding='utf-8'))['events'])


class WaterLoopTests(unittest.TestCase):

    # ---------- 题10基线与表级原语 ----------

    def test_seed_baseline_ten(self):
        from rangeledger.water_ledger import used
        self.assertEqual(used([100, 105, 110]), 10)
        self.assertEqual(delta(100, 110), 10)        # delta 直接 after-before
        self.assertEqual(delta(110, 100), -10)      # 方向不可翻转

    def test_every_fixture_event_is_signed_and_verifies(self):
        events = load_events()
        self.assertTrue(all(verify_event(e) for e in events))
        tampered = deepcopy(events[0])
        tampered['data']['tampered'] = True
        self.assertFalse(verify_event(tampered))
        self.assertTrue(verify_event(events[0]))    # 原始证据不被改写

    def test_meter_deltas_sorted_deduped_and_reset_aware(self):
        events = load_events()
        # M-ML: 首表走到5后换表归零，新表0读数与离线重复补传都不得增量
        self.assertEqual([row[1] for row in meter_deltas(events, 'M-ML')], [5.0, 0.0])
        self.assertEqual(meter_volume(events, 'M-ML'), 5.0)
        # 多读数表相邻差求和
        self.assertEqual(meter_volume(events, 'M-IN'), 100.0)
        self.assertEqual(meter_volume(events, 'M-BR'), 50.0)

    def test_all_nine_meters_match_fixed_network(self):
        self.assertEqual(all_meter_volumes(load_events()), {
            'M-IN': 100.0, 'M-A': 40.0, 'M-BR': 50.0, 'M-BD': 30.0, 'M-RET': 20.0,
            'M-DIR': 5.0, 'M-ML': 5.0, 'M-FO': 18.0, 'M-FL': 2.0,
        })

    def test_unsigned_reading_is_excluded_from_volume(self):
        events = load_events()
        events.append(dict(id='m-A-rogue', kind='meter_reading', subject=SUBJECT,
                           event_time=99, received_at=99,
                           data=dict(meter_id='M-A', reading=1234, prior_reading=40)))  # 未签名
        self.assertFalse(verify_event(events[-1]))
        self.assertEqual(meter_volume(events, 'M-A'), 40.0)

    def test_calibration_factor_corrects_fast_meter(self):
        events = load_events()
        # M-FL 表偏快：原始相邻差之和 20/9，校准因子0.9 后恰为2方
        raw = sum(row[1] for row in meter_deltas(events, 'M-FL'))
        self.assertAlmostEqual(raw, 20 / 9, places=6)
        self.assertAlmostEqual(meter_volume(events, 'M-FL'), 2.0, places=6)

    # ---------- 120 反例：常规采集全对，但图语义错误 ----------

    def test_naive_return_as_new_intake_books_120(self):
        self.assertEqual(naive_return_as_new_intake(load_events()), 120.0)

    def test_naive_full_branch_booking_books_120(self):
        self.assertEqual(naive_branch_fully_booked(load_events()), 120.0)

    def test_return_meter_is_evidence_not_noise(self):
        events = load_events()
        self.assertTrue(return_path_present(events))
        # 20 -> 18 + 2 正是滤池损耗2的定位证据，删表即丢失
        self.assertEqual(meter_volume(events, 'M-RET'), 20.0)
        self.assertEqual(meter_volume(events, 'M-FO') + meter_volume(events, 'M-FL'), 20.0)

    # ---------- 100 = 40 + 30 + 23 + 7 节点守恒 ----------

    def test_every_node_closes_within_one_cubic_meter(self):
        rec = reconcile(load_events(), TOL)
        self.assertEqual(rec['frozen'], [])
        for node, info in rec['nodes'].items():
            self.assertLessEqual(abs(info['discrepancy']), TOL, node)
            self.assertTrue(info['meters'], node)

    def test_global_identity_100_equals_consumption_storage_leakage(self):
        t = totals(load_events())
        self.assertEqual(t['external_intake'], 100.0)
        self.assertEqual(t['consumed'], 70.0)
        self.assertEqual(t['consumed_by_holder'], {'A': 40.0, 'B': 30.0})
        self.assertEqual(t['storage_increase'], 23.0)
        self.assertEqual(t['reservoir_level_increase'], 23.0)   # 水位 (7.3-5.0)*10
        self.assertEqual(t['leakage'], 7.0)
        self.assertEqual(t['closed'], 100.0)
        self.assertEqual(t['internal_return'], 20.0)            # 回流是内部边，不计外部进水
        self.assertEqual(t['external_intake'], t['closed'])

    def test_b_branch_split_30_drunk_20_internal_transfer(self):
        events = load_events()
        pipes = build_pipes(events)
        self.assertEqual(pipes['e-b-drink']['holder'], 'B')
        self.assertEqual(pipes['e-return']['role'], 'return')
        self.assertEqual(meter_volume(events, 'M-BD'), 30.0)
        self.assertEqual(meter_volume(events, 'M-RET'), 20.0)
        self.assertEqual(meter_volume(events, 'M-BR'), 50.0)    # 50 = 30 实喝 + 20 内部转移

    # ---------- 冻结：差额超1方只冻结该节点，不摊给所有牧户 ----------

    def test_imbalance_over_tolerance_freezes_only_touching_nodes(self):
        events = load_events()
        # B 饮水表补来正确签名但多走2方的读数：junction-b 入50、出32+20 超差，
        # A 饮水点与该差额没有任何边相连，绝不被摊账或连带冻结。
        events.append(sign_event(dict(
            id='m-BD-extra', kind='meter_reading', subject=SUBJECT,
            event_time=40, received_at=180,
            data=dict(meter_id='M-BD', reading=32, prior_reading=30))))
        rec = reconcile(events, TOL)
        self.assertIn('junction-b', rec['frozen'])
        self.assertNotIn('drink-a', rec['frozen'])
        self.assertNotIn('header', rec['frozen'])
        self.assertIn('M-BD', rec['nodes']['junction-b']['meters'])
        self.assertGreater(abs(rec['nodes']['junction-b']['discrepancy']), TOL)
        issued = issue_vouchers(events, rec)
        for proto in (INFRASTRUCTURE, JOINT_STEWARDSHIP):
            # 只有 B 的凭证被冻结；A 的收费体积照常出具，差额没有摊给 A 的实耗
            self.assertEqual(issued[proto]['blocked_holders'], ['B'])
            self.assertTrue(issued[proto]['bills']['A']['issued'])
            self.assertFalse(issued[proto]['bills']['B']['issued'])
        # 维护协议：管理方承担漏损，A 仍只付实耗40
        self.assertEqual(issued[INFRASTRUCTURE]['bills']['A']['billed_volume'], 40.0)
        # 共同管护：B 被冻结后不参与分摊，7方漏损由唯一有效牧户 A 承担（40+7）
        self.assertEqual(issued[JOINT_STEWARDSHIP]['bills']['A']['billed_volume'], 47.0)
        self.assertEqual(issued[JOINT_STEWARDSHIP]['bills']['A']['leakage_share'], 7.0)
        # A 饮水节点守恒差额仍为0，实耗始终是40，没有被改写
        self.assertEqual(rec['nodes']['drink-a']['discrepancy'], 0.0)
        self.assertEqual(issued[INFRASTRUCTURE]['bills']['A']['actual_consumption'], 40.0)

    def test_header_freeze_blocks_all_connected_holders_but_never_redistributes(self):
        events = load_events()
        # 主管漏表多走2方：共同上游 header 冻结，A/B 凭证都冻结，
        # 但任何牧户的实耗体积都不被改写（冻结而非摊派）。
        events.append(sign_event(dict(
            id='m-ML-extra', kind='meter_reading', subject=SUBJECT,
            event_time=40, received_at=180,
            data=dict(meter_id='M-ML', reading=7, prior_reading=5))))
        rec = reconcile(events, TOL)
        self.assertEqual(rec['frozen'], ['header'])
        issued = issue_vouchers(events, rec)
        for proto in (INFRASTRUCTURE, JOINT_STEWARDSHIP):
            self.assertEqual(sorted(issued[proto]['blocked_holders']), ['A', 'B'])
            # 实耗字段保留物理真值，没有把2方漏损塞进任何牧户账
            self.assertEqual(issued[proto]['bills']['A']['actual_consumption'], 40.0)
            self.assertEqual(issued[proto]['bills']['B']['actual_consumption'], 30.0)
            self.assertIsNone(issued[proto]['bills']['A']['billed_volume'])
        self.assertEqual(rec['nodes']['drink-a']['discrepancy'], 0.0)
        self.assertEqual(rec['nodes']['drink-b']['discrepancy'], 0.0)

    def test_bad_signature_reading_freezes_its_nodes_without_spreading(self):
        events = load_events()
        events.append(dict(id='m-BD-rogue', kind='meter_reading', subject=SUBJECT,
                           event_time=41, received_at=181,
                           data=dict(meter_id='M-BD', reading=999, prior_reading=30)))  # 未签名
        rec = reconcile(events, TOL)
        self.assertIn('junction-b', rec['frozen'])
        self.assertIn('drink-b', rec['frozen'])
        self.assertNotIn('drink-a', rec['frozen'])
        self.assertIn('m-BD-rogue', ' '.join(rec['nodes']['drink-b']['reasons']))
        # 未签名读数不进入体积，B 实耗仍是30；冻结的是节点而不是改写水量账
        self.assertEqual(meter_volume(events, 'M-BD'), 30.0)
        issued = issue_vouchers(events, rec)
        self.assertFalse(issued[JOINT_STEWARDSHIP]['bills']['B']['issued'])
        self.assertTrue(issued[JOINT_STEWARDSHIP]['bills']['A']['issued'])

    def test_missing_meter_freezes_its_edge_nodes_only(self):
        events = [e for e in load_events()
                  if not (e.get('kind') == 'meter_reading' and e['data'].get('meter_id') == 'M-FO')]
        rec = reconcile(events, TOL)
        self.assertIn('filter', rec['frozen'])
        self.assertIn('reservoir', rec['frozen'])
        self.assertNotIn('drink-a', rec['frozen'])

    def test_valve_closed_with_flow_is_a_freeze_reason(self):
        events = load_events()
        events.append(sign_event(dict(
            id='valve-e-a-force-close', kind='valve_action', subject=SUBJECT,
            event_time=90, received_at=90, data=dict(edge_id='e-a', open=False))))
        rec = reconcile(events, TOL)
        self.assertIn('drink-a', rec['frozen'])
        self.assertTrue(any(r.startswith('valve_closed_but_flowing')
                            for r in rec['nodes']['drink-a']['reasons']))

    # ---------- 两种经签认的归责凭证：同一物理100，不同收费体积 ----------

    def test_two_protocols_issue_distinct_signed_vouchers(self):
        events = load_events()
        issued = issue_vouchers(events, reconcile(events, TOL))
        infra, joint = issued[INFRASTRUCTURE], issued[JOINT_STEWARDSHIP]
        self.assertTrue(infra['issued'] and joint['issued'])
        for key in ('external_intake', 'consumed', 'storage_increase', 'leakage'):
            self.assertEqual(infra['physical'][key], joint['physical'][key])
        # 维护协议：管理方承担7，牧户只付实耗
        self.assertEqual(infra['bills']['A']['billed_volume'], 40.0)
        self.assertEqual(infra['bills']['B']['billed_volume'], 30.0)
        self.assertEqual(infra['bills']['A']['leakage_share'], 0.0)
        self.assertEqual(infra['manager']['fee_bearing_leakage'], 7.0)
        # 共同管护：7 按 40:30 分摊，A=4、B=3，管理方只登记物理漏损不付水费
        self.assertEqual(joint['bills']['A'], dict(
            issued=True, actual_consumption=40.0, billed_volume=44.0, leakage_share=4.0,
            replenishment_budget=44.0, frozen_nodes=[],
            evidence_ids=joint['bills']['A']['evidence_ids']))
        self.assertEqual(joint['bills']['B']['billed_volume'], 33.0)
        self.assertEqual(joint['bills']['B']['leakage_share'], 3.0)
        self.assertEqual(joint['manager']['fee_bearing_leakage'], 0.0)
        self.assertEqual(joint['manager']['physical_leakage_registered'], 7.0)
        # 凭证必须引用签认事件与原始读数，不能只是报表标签
        self.assertTrue(joint['signoff_event_id'].startswith('signoff-joint'))
        self.assertTrue(all(b['evidence_ids'] for b in joint['bills'].values()))

    def test_no_signoff_no_voucher(self):
        events = [e for e in load_events() if e.get('kind') != 'liability_signoff']
        issued = issue_vouchers(events)
        self.assertFalse(issued[INFRASTRUCTURE]['issued'])
        self.assertFalse(issued[JOINT_STEWARDSHIP]['issued'])
        self.assertEqual(issued[JOINT_STEWARDSHIP]['reason'], 'missing_signed_protocol')

    def test_voucher_versions_are_retained_in_store(self):
        events = load_events()
        rec = reconcile(events, TOL)
        with tempfile.TemporaryDirectory() as td:
            db = connect(Path(td) / 'water.db')
            with db:
                for event in events:
                    ingest(db, event)
                save_node_balances(db, SUBJECT, events, rec)
                for proto, voucher in issue_vouchers(events, rec).items():
                    save_voucher(db, SUBJECT, proto, voucher, voucher['signoff_event_id'])
            first = vouchers(db, SUBJECT)
            self.assertEqual(sorted(first), [INFRASTRUCTURE, JOINT_STEWARDSHIP])
            # 再算一遍内容不变 -> 不新增版本
            with db:
                for proto, voucher in issue_vouchers(events, rec).items():
                    save_voucher(db, SUBJECT, proto, voucher, voucher['signoff_event_id'])
            self.assertTrue(all(x['version'] == 1 for x in vouchers(db, SUBJECT).values()))
            # 共同管护协议新版本签认，只升该协议版本，维护协议与原始证据保留
            events.append(sign_event(dict(
                id='signoff-joint-2', kind='liability_signoff', subject=SUBJECT,
                event_time=70, received_at=70,
                data=dict(protocol=JOINT_STEWARDSHIP, active=True, signers=['manager', 'A', 'B']))))
            with db:
                ingest(db, events[-1])
                rec2 = reconcile(events, TOL)
                save_node_balances(db, SUBJECT, events, rec2)
                save_voucher(db, SUBJECT, JOINT_STEWARDSHIP,
                             issue_vouchers(events, rec2, JOINT_STEWARDSHIP)[JOINT_STEWARDSHIP],
                             'signoff-joint-2')
            latest = vouchers(db, SUBJECT)
            self.assertEqual(latest[JOINT_STEWARDSHIP]['version'], 2)
            self.assertEqual(latest[INFRASTRUCTURE]['version'], 1)
            self.assertEqual(len(evidence(db, SUBJECT)), len(events))
            db.close()

    # ---------- 迟到水表与阀门记录：只重算接触节点 ----------

    def test_late_meter_reading_impacts_only_its_edge_nodes(self):
        events = load_events()
        before = reconcile(events, TOL)
        events.append(sign_event(dict(
            id='m-A-late', kind='meter_reading', subject=SUBJECT,
            event_time=45, received_at=200,        # 迟到：事件时刻早、接收时刻晚
            data=dict(meter_id='M-A', reading=42, prior_reading=40))))
        self.assertEqual(impacted_nodes(events, ['m-A-late']), ['drink-a', 'header'])
        after = reconcile(events, TOL)
        for node, info in before['nodes'].items():
            if node not in ('drink-a', 'header'):
                self.assertEqual(after['nodes'][node]['discrepancy'], info['discrepancy'])
        # 迟到2方在有独立入出流仪表的 header 节点超差冻结；B、滤池、蓄水池等无关节点不动
        self.assertEqual(after['frozen'], ['header'])
        self.assertGreater(abs(after['nodes']['header']['discrepancy']), TOL)

    def test_late_valve_event_does_not_touch_unrelated_nodes(self):
        events = load_events()
        events.append(sign_event(dict(
            id='valve-return-late', kind='valve_action', subject=SUBJECT,
            event_time=22, received_at=300, data=dict(edge_id='e-return', open=True))))
        self.assertEqual(impacted_nodes(events, ['valve-return-late']), ['filter', 'junction-b'])
        self.assertEqual(reconcile(events, TOL)['frozen'], [])

    def test_store_replay_from_raw_evidence_reproduces_balances(self):
        events = load_events()
        with tempfile.TemporaryDirectory() as td:
            db = connect(Path(td) / 'water.db')
            with db:
                for event in events:
                    ingest(db, event)
                rec = reconcile(events, TOL)
                save_node_balances(db, SUBJECT, events, rec)
            stored = report(db, SUBJECT)
            # 从库里原始证据按 subject 重算，必须复现每个节点的守恒差额
            replayed = reconcile(evidence(db, SUBJECT), TOL)
            for node, info in replayed['nodes'].items():
                surface = f'水量守恒:{node}'
                self.assertEqual(stored[surface]['result']['discrepancy'], info['discrepancy'])
                self.assertEqual(stored[surface]['result']['frozen'], info['frozen'])
            db.close()


if __name__ == '__main__':
    unittest.main()
