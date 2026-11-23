"""五代祖谱、近交系数门槛反转、改配/亲权更正两路与可追溯性验收。"""

import json
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from rangeledger.breeding import (
    DEFAULT_THRESHOLD,
    KinshipCalculator,
    apply_parentage_review,
    build_studbook,
    evaluate_pair,
    inbreeding_coefficient,
    issue,
    pedigree_events,
    projected_inbreeding,
    serialize_result,
    shared_parent_count,
)
from rangeledger.studbook import (
    AliasConflictError,
    PedigreeCycleError,
    PedigreeValidationError,
    Studbook,
    UnknownIdentityError,
)
from rangeledger.store import (
    breeding_history,
    connect,
    evidence,
    ingest,
    latest_pedigree_snapshot,
    pedigree_versions,
    save_breeding_decision,
    save_pedigree_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'breeding-pedigree.json'
SUBJECT = 'studbook-case-1'


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def baseline_book():
    """消费原始谱系证据建册；更正场景事件留给路线测试单独应用。"""
    fixture = load_fixture()
    events = pedigree_events(fixture['events'], scenario='baseline')
    book, stats = build_studbook(events)
    return book, stats, events, fixture


class FiveGenerationPedigreeTests(unittest.TestCase):
    def setUp(self):
        self.book, self.stats, self.events, self.fixture = baseline_book()

    def test_full_five_generation_pedigree_loads(self):
        # G/H/J/K/L 创始个体；P、Q；X；A、B；拟产犊为第五代
        founders = {'G', 'H', 'J', 'K', 'L'}
        self.assertEqual(set(self.book.ids()),
                         founders | {'P', 'Q', 'X', 'A', 'B', 'C', 'N', 'O'})
        self.assertEqual(self.book.generation_count(), 4)
        self.assertEqual(self.book.depth('G'), 0)
        self.assertEqual(self.book.depth('P'), 1)
        self.assertEqual(self.book.depth('X'), 2)
        self.assertEqual(self.book.depth('A'), 3)
        evaluation = evaluate_pair(self.book, 'A', 'B')
        self.assertEqual(evaluation['projected_generation'], 5)

    def test_each_parent_edge_carries_source_evidence(self):
        edge = self.book.edge('X', 'P')
        self.assertEqual(edge['status'], 'active')
        self.assertEqual(edge['role'], 'sire')
        self.assertIn('pedigree-X', edge['evidence_ids'])
        dam_edge = self.book.edge('Q', 'J')
        self.assertEqual(dam_edge['role'], 'dam')
        self.assertIn('pedigree-Q', dam_edge['evidence_ids'])

    def test_topological_order_respects_parent_before_child(self):
        order = self.book.topological_order()
        position = {node: index for index, node in enumerate(order)}
        for child in self.book.ids():
            for parent in self.book.parents(child):
                if parent:
                    self.assertLess(position[parent], position[child])

    def test_shared_parent_baseline_still_one(self):
        # 既有基线语义不能回退：A/B 同父 X
        animals = {
            node: {key: value for key, value in self.book.animal(node).items()
                   if key in ('sire', 'dam') and value}
            for node in self.book.ids()
        }
        self.assertEqual(shared_parent_count(animals, 'A', 'B'), 1)

    def test_unknown_identity_rejected(self):
        with self.assertRaises(UnknownIdentityError):
            self.book.resolve('不存在的耳标')
        with self.assertRaises(UnknownIdentityError):
            Studbook().add_animal('Z', sire='GHOST')

    def test_birth_time_must_follow_parents(self):
        book = Studbook()
        book.add_animal('F1', born=10)
        with self.assertRaises(PedigreeValidationError):
            book.add_animal('CHILD', sire='F1', born=9)


class InbreedingCoefficientTests(unittest.TestCase):
    def setUp(self):
        self.book, *_ = baseline_book()
        self.calculator = KinshipCalculator(self.book)

    def test_x_is_inbred_one_eighth(self):
        self.assertEqual(inbreeding_coefficient(self.book, 'X'),
                         Fraction(1, 8))

    def test_founder_inbreeding_is_zero(self):
        for founder in ('G', 'H', 'J', 'K', 'L'):
            self.assertEqual(inbreeding_coefficient(self.book, founder),
                             Fraction(0))

    def test_ab_projected_inbreeding_is_9_over_64(self):
        # (1 + F(X)) / 8 = 9/64，重复祖先路径由递推自动合并
        self.assertEqual(projected_inbreeding(self.book, 'A', 'B'),
                         Fraction(9, 64))

    def test_wright_decomposition_matches_recursive_phi(self):
        decomposition = self.calculator.wright_decomposition('A', 'B')
        self.assertTrue(decomposition['matches'])
        self.assertEqual(Fraction(*decomposition['recursive_phi']),
                         Fraction(9, 64))
        valid = [term for term in decomposition['terms'] if term['valid']]
        invalid = [term for term in decomposition['terms']
                   if not term['valid']]
        # 经共同祖先 X 的路径 A-X 与 B-X 共 3 条边：(1+F(X))/2^3 = 9/64
        x_terms = [term for term in valid if term['ancestor'] == 'X']
        self.assertEqual(len(x_terms), 1)
        self.assertEqual(x_terms[0]['edges'], 3)
        self.assertEqual(
            sum((Fraction(*term['contribution']) for term in x_terms),
                Fraction(0)),
            Fraction(9, 64))
        # 还存在经 G/H/J/K/L 等的路径项，其中重叠路径被剔除
        self.assertGreater(len(decomposition['terms']), 1)
        # 必须存在被剔除的重叠路径，证明不能按共同祖先数量直接计数
        self.assertTrue(invalid)

    def test_unrelated_pair_c_has_zero_coefficient(self):
        self.assertEqual(projected_inbreeding(self.book, 'A', 'C'),
                         Fraction(0))

    def test_calculator_survives_later_animal_registration(self):
        book = Studbook()
        book.add_animal('F')
        book.add_animal('C1', sire='F')
        calculator = KinshipCalculator(book)
        self.assertEqual(calculator.phi('C1', 'F'), Fraction(1, 4))
        # 计算器构造之后再登记新个体，排名缓存必须惰性重建
        book.add_animal('Z')
        self.assertEqual(calculator.phi('F', 'Z'), Fraction(0))

    def test_calculator_invalidates_after_parent_replacement(self):
        book = Studbook()
        book.add_animal('G')
        book.add_animal('J')
        book.add_animal('M')
        book.add_animal('Q', sire='G', dam='J')
        calculator = KinshipCalculator(book)
        self.assertEqual(calculator.phi('Q', 'G'), Fraction(1, 4))
        book.replace_parent('Q', 'sire', 'M', 'review-1')
        # 修订后旧共祖缓存必须失效：G 已无亲属关系，M 成为父本
        self.assertEqual(calculator.phi('Q', 'G'), Fraction(0))
        self.assertEqual(calculator.phi('Q', 'M'), Fraction(1, 4))

    def test_full_sib_mating_is_one_quarter(self):
        book = Studbook()
        book.add_animal('S')
        book.add_animal('D')
        book.add_animal('O1', sire='S', dam='D')
        book.add_animal('O2', sire='S', dam='D')
        self.assertEqual(projected_inbreeding(book, 'O1', 'O2'),
                         Fraction(1, 4))


class ThresholdAndRouteTests(unittest.TestCase):
    def setUp(self):
        self.book, _, self.events, self.fixture = baseline_book()
        self.threshold = Fraction(*self.fixture['threshold'])
        self.assertEqual(self.threshold, DEFAULT_THRESHOLD)

    def test_route_one_reject_ab_and_issue_ac(self):
        rejected = evaluate_pair(self.book, 'A', 'B', self.threshold)
        self.assertEqual(rejected['decision'], '拒签')
        self.assertEqual(rejected['coefficient'], Fraction(9, 64))
        self.assertGreater(Fraction(9, 64), Fraction(13, 100))

        rerouted = evaluate_pair(self.book, 'A', 'C', self.threshold)
        self.assertEqual(rerouted['decision'], '签发')
        self.assertEqual(rerouted['coefficient'], Fraction(0))

    def test_route_two_parentage_review_flips_decision(self):
        # 原始证据下拒签
        baseline = evaluate_pair(self.book, 'A', 'B', self.threshold)
        self.assertEqual(baseline['decision'], '拒签')

        all_events = pedigree_events(self.fixture['events'])
        review = next(event for event in all_events
                      if event['kind'] == 'parentage_review')
        corrected_book, _ = build_studbook(all_events)
        apply_parentage_review(corrected_book, review)
        self.assertEqual(corrected_book.resolve('M'), 'M')

        self.assertEqual(inbreeding_coefficient(corrected_book, 'X'),
                         Fraction(0))
        reviewed = evaluate_pair(corrected_book, 'A', 'B', self.threshold)
        self.assertEqual(reviewed['coefficient'], Fraction(1, 8))
        self.assertLessEqual(Fraction(1, 8), Fraction(13, 100))
        self.assertEqual(reviewed['decision'], '签发')

        # 更正保留旧边，不覆写 P/Q/G 关联
        old_edge = corrected_book.edge('Q', 'G')
        self.assertEqual(old_edge['status'], 'superseded')
        self.assertEqual(old_edge['superseded_by'], review['id'])
        new_edge = corrected_book.edge('Q', 'M')
        self.assertEqual(new_edge['status'], 'active')
        self.assertEqual(new_edge['replaces'], 'G')

    def test_review_without_new_founder_is_unknown_identity(self):
        all_events = pedigree_events(self.fixture['events'])
        review = next(event for event in all_events
                      if event['kind'] == 'parentage_review')
        # 基线册尚未登记新创始个体 M，更正必须被拒绝而不是凭空造个体
        with self.assertRaises(UnknownIdentityError):
            apply_parentage_review(self.book, review)


class CycleAndAliasTests(unittest.TestCase):
    def test_pedigree_cycle_rejected(self):
        book = Studbook()
        book.add_animal('U1')
        book.add_animal('U2')
        book.add_animal('U3')
        book.add_animal('U4', sire='U1', dam='U2')
        book.add_animal('U5', sire='U4', dam='U3')
        # 让 U1 的父亲指向后代 U5，构成环
        with self.assertRaises(PedigreeCycleError):
            book.add_animal('U1', sire='U5')
        self.assertIsNone(book.animal('U1')['sire'])

    def test_replace_parent_cycle_rejected(self):
        book = Studbook()
        book.add_animal('V0')
        book.add_animal('V1', sire='V0')
        book.add_animal('V2', sire='V1')
        book.add_animal('V3', sire='V2')
        # 把 V1 的父本改成自己的后代 V3，构成环且原边保持不变
        with self.assertRaises(PedigreeCycleError):
            book.replace_parent('V1', 'sire', 'V3', 'review-cycle')
        self.assertEqual(book.parents('V1'), ('V0', None))
        self.assertEqual(book.edge('V1', 'V0')['status'], 'active')

    def test_self_parent_rejected(self):
        book = Studbook()
        book.add_animal('W1')
        with self.assertRaises(PedigreeCycleError):
            book.add_animal('W1', sire='W1')

    def test_same_sire_and_dam_rejected(self):
        book = Studbook()
        book.add_animal('E1')
        with self.assertRaises(PedigreeValidationError):
            book.add_animal('E2', sire='E1', dam='E1')

    def test_alias_conflict_rejected(self):
        book = Studbook()
        book.add_animal('D1', aliases=['耳标-9'])
        with self.assertRaises(AliasConflictError):
            book.add_animal('D2', aliases=['耳标-9'])

    def test_alias_resolves_to_canonical_and_not_double_counted(self):
        book, *_ = baseline_book()
        self.assertEqual(book.resolve('母畜A'), 'A')
        self.assertEqual(book.resolve('TAG-B'), 'B')
        # 用别名计算与用规范 ID 计算必须完全一致，不会重复计数
        direct = projected_inbreeding(book, 'A', 'B')
        via_alias = projected_inbreeding(book, '母畜A', '公畜B')
        self.assertEqual(direct, via_alias)
        self.assertEqual(evaluate_pair(book, 'TAG-A', 'TAG-B')['pair'],
                         ['A', 'B'])
        # 再给 A 增加一个别名，系数不得发生任何变化
        book.add_animal('A', aliases=['新增耳标-A'])
        self.assertEqual(book.resolve('新增耳标-A'), 'A')
        self.assertEqual(projected_inbreeding(book, '新增耳标-A', 'B'),
                         Fraction(9, 64))


class IdempotenceAndTraceabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'studbook.db')
        self.book, self.stats, self.events, self.fixture = baseline_book()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _ingest_all(self, events):
        with self.db:
            for event in events:
                ingest(self.db, event)
        return evidence(self.db, SUBJECT)

    def test_duplicate_import_is_idempotent(self):
        first_book, _ = build_studbook(self.events)
        second_book, second_stats = build_studbook(self.events)
        first_snapshot = first_book.snapshot('s1', SUBJECT, 1)
        second_snapshot = second_book.snapshot('s2', SUBJECT, 1)
        first_snapshot.pop('snapshot_id')
        second_snapshot.pop('snapshot_id')
        self.assertEqual(first_snapshot, second_snapshot)
        self.assertEqual(second_stats['skipped'], 0)
        # 同一册再次导入：全部跳过，且计算结果不变
        repeat_stats = first_book.load_events(self.events)
        pedigree_count = sum(1 for event in self.events
                             if event['kind'] == 'pedigree')
        self.assertEqual(repeat_stats['skipped'], pedigree_count)
        self.assertEqual(repeat_stats['added'], 0)
        self.assertEqual(
            projected_inbreeding(first_book, 'A', 'B'), Fraction(9, 64))

    def test_conflicting_reraises(self):
        book, _ = build_studbook(self.events)
        tampered = [dict(event) for event in self.events]
        tampered[0] = dict(tampered[0], data=dict(
            tampered[0]['data'], born=99))
        with self.assertRaises(PedigreeValidationError):
            build_studbook(tampered)

    def test_snapshot_and_breeding_versions_remain_traceable(self):
        events = self._ingest_all(self.events)
        threshold = Fraction(*self.fixture['threshold'])
        with self.db:
            snap1 = self.book.snapshot(
                'snapshot-baseline', SUBJECT, 1,
                rationale='原始谱系证据建册',
                evidence_ids=[event['id'] for event in events])
            save_pedigree_snapshot(self.db, SUBJECT, snap1, 50)

            baseline_issue = issue(
                self.book, 'A', 'B', threshold,
                evidence_ids=['pedigree-A', 'pedigree-B', 'pedigree-X'])
            version1 = save_breeding_decision(self.db, SUBJECT, baseline_issue)
            self.assertEqual(version1, 1)
            # 幂等：相同结果不产生新版
            self.assertEqual(
                save_breeding_decision(self.db, SUBJECT, baseline_issue), 1)

            reroute = issue(self.book, 'A', 'C', threshold,
                            evidence_ids=['pedigree-A', 'pedigree-C'])
            save_breeding_decision(self.db, SUBJECT, reroute)

        history = breeding_history(self.db, SUBJECT, 'AxB')
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['result']['decision'], '拒签')
        self.assertEqual(Fraction(*history[0]['result']['coefficient']),
                         Fraction(9, 64))

        # 亲权复核：新快照 + 新决定版本，旧计算保留
        all_events = pedigree_events(self.fixture['events'])
        review = next(event for event in all_events
                      if event['kind'] == 'parentage_review')
        corrected_book, _ = build_studbook(all_events)
        apply_parentage_review(corrected_book, review)
        with self.db:
            snap2 = corrected_book.snapshot(
                'snapshot-reviewed', SUBJECT, 2,
                rationale='签名基因亲权复核：Q 父本 G 更正为 M',
                parent_snapshot_id='snapshot-baseline',
                evidence_ids=['pedigree-M', review['id']])
            save_pedigree_snapshot(self.db, SUBJECT, snap2, 70)
            reviewed_issue = issue(
                corrected_book, 'A', 'B', threshold,
                evidence_ids=['pedigree-A', 'pedigree-B', review['id']])
            version2 = save_breeding_decision(
                self.db, SUBJECT, reviewed_issue)
        self.assertEqual(version2, 2)

        versions = pedigree_versions(self.db, SUBJECT)
        self.assertEqual([item['version'] for item in versions], [1, 2])
        self.assertEqual(versions[1]['snapshot']['parent_snapshot_id'],
                         'snapshot-baseline')
        rebuilt = Studbook.from_snapshot(versions[1]['snapshot'])
        self.assertEqual(inbreeding_coefficient(rebuilt, 'X'), Fraction(0))
        self.assertEqual(
            projected_inbreeding(rebuilt, 'A', 'B'), Fraction(1, 8))
        # 旧快照里仍然是 9/64 与旧边，未被覆写
        rebuilt_old = Studbook.from_snapshot(versions[0]['snapshot'])
        self.assertEqual(
            projected_inbreeding(rebuilt_old, 'A', 'B'), Fraction(9, 64))
        self.assertEqual(rebuilt_old.edge('Q', 'G')['status'], 'active')
        self.assertEqual(rebuilt.edge('Q', 'G')['status'], 'superseded')

        full_history = breeding_history(self.db, SUBJECT, 'AxB')
        self.assertEqual(len(full_history), 2)
        self.assertEqual(
            Fraction(*full_history[0]['result']['coefficient']),
            Fraction(9, 64))
        self.assertEqual(
            Fraction(*full_history[1]['result']['coefficient']),
            Fraction(1, 8))
        self.assertEqual(full_history[0]['result']['decision'], '拒签')
        self.assertEqual(full_history[1]['result']['decision'], '签发')
        self.assertEqual(latest_pedigree_snapshot(
            self.db, SUBJECT)['version'], 2)

    def test_snapshot_same_version_conflict_rejected(self):
        snap = self.book.snapshot('snapshot-a', SUBJECT, 1)
        with self.db:
            save_pedigree_snapshot(self.db, SUBJECT, snap, 50)
        snap['rationale'] = '被篡改的说明'
        with self.assertRaises(ValueError):
            save_pedigree_snapshot(self.db, SUBJECT, snap, 51)

    def test_serialized_result_is_json_safe(self):
        evaluation = evaluate_pair(self.book, 'A', 'B')
        encoded = json.dumps(serialize_result(evaluation),
                             ensure_ascii=False)
        decoded = json.loads(encoded)
        self.assertEqual(decoded['coefficient'], [9, 64])
        self.assertEqual(decoded['threshold'], [13, 100])


if __name__ == '__main__':
    unittest.main()
