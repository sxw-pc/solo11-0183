import json
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from rangeledger.cohort_stats import (
    isolable_holders,
    joint_review,
    member_vector,
    release_matrix,
)
from rangeledger.common import pressure
from rangeledger.public_report import release, release_reports
from rangeledger.store import (
    budget_account,
    connect,
    evidence,
    ingest,
    public_releases,
    release_access_log,
    set_budget,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / 'fixtures' / 'pasture-public.json').read_text(encoding='utf-8'))
SUBJECT = FIXTURE['subject']
UNIVERSE = FIXTURE['universe']
HOURS = FIXTURE['expected_hours']
REQUESTS = FIXTURE['requests']
TABLES = ['ABC', 'BCD', 'CDE', 'DEA', 'EAB']
EXACT_TOTALS = {'ABC': 60, 'BCD': 90, 'CDE': 120, 'DEA': 100, 'EAB': 80}


def solve(matrix, values):
    """Exact solution of a square linear system over the rationals."""
    n = len(matrix)
    rows = [[Fraction(v) for v in row] + [Fraction(str(values[i]))] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = next(i for i in range(col, n) if rows[i][col] != 0)
        rows[col], rows[pivot] = rows[pivot], rows[col]
        for i in range(n):
            if i != col and rows[i][col] != 0:
                factor = rows[i][col] / rows[col][col]
                rows[i] = [v - factor * p for v, p in zip(rows[i], rows[col])]
    return [rows[i][n] / rows[i][i] for i in range(n)]


def correction(event_id, holder, herd, end, event_time):
    return dict(id=event_id, kind='movement', subject=SUBJECT, event_time=event_time,
                received_at=event_time,
                data={'holder': holder, 'herd': herd, 'zone': 'P1', 'start': 0, 'end': end,
                      'count': 1, 'month': '2026-10', 'coop': 'coop-1'})


class PasturePrivacyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'case.db')
        with self.db:
            for event in FIXTURE['events']:
                ingest(self.db, event)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def vectors(self, names=TABLES):
        members = {r['table']: r['members'] for r in REQUESTS}
        return [member_vector(members[name], UNIVERSE) for name in names]

    def test_seed_baseline_abc60(self):
        rows = [{'holder': k, 'animal_hours': v} for k, v in [('A', 10), ('B', 20), ('C', 30)]]
        self.assertEqual(release(rows), {'holders': 3, 'animal_hours': 60})

    def test_five_qualified_tables_reconstruct_every_holder(self):
        # The legacy headcount check waves every one of the five tables through.
        members = {r['table']: r['members'] for r in REQUESTS}
        for name in TABLES:
            rows = [{'holder': h, 'animal_hours': HOURS[h]} for h in members[name]]
            self.assertEqual(release(rows), {'holders': 3, 'animal_hours': EXACT_TOTALS[name]})
        # No single-herder query was ever made, yet the five public sums solve exactly.
        solution = solve(self.vectors(), [EXACT_TOTALS[name] for name in TABLES])
        self.assertEqual([int(v) for v in solution], [10, 20, 30, 40, 50])
        # Joint review: four tables isolate nobody, the fifth isolates everyone.
        vectors = self.vectors()
        self.assertEqual(isolable_holders(release_matrix(vectors[:4]), UNIVERSE), set())
        review = joint_review(vectors[:4], vectors[4], UNIVERSE)
        self.assertFalse(review['allowed'])
        self.assertEqual(review['isolable'], UNIVERSE)
        # Raising the minimum to 4 does not fix cross-table reversibility:
        # five 4-holder tables (J - I) still have full rank.
        four_holder = [[1, 1, 1, 1, 0], [0, 1, 1, 1, 1], [1, 0, 1, 1, 1],
                       [1, 1, 0, 1, 1], [1, 1, 1, 0, 1]]
        self.assertEqual(isolable_holders(release_matrix(four_holder), UNIVERSE), set(UNIVERSE))

    def test_fixed_groups_protocol(self):
        result = release_reports(self.db, SUBJECT, REQUESTS, protocol='fixed-groups',
                                 grouping=FIXTURE['grouping'], minimum=FIXTURE['minimum'])
        abc = result['tables']['ABC']
        self.assertEqual(abc['status'], 'published')
        self.assertEqual(abc['payload'], {'holders': 3, 'animal_hours': 60})
        self.assertEqual(abc['member_vector'], [1, 1, 1, 0, 0])
        self.assertEqual(abc['evidence_ids'], ['pub-move-A', 'pub-move-B', 'pub-move-C'])
        # The other exact cross tables are refused: they would jointly isolate herders.
        for name in ('BCD', 'CDE', 'DEA', 'EAB'):
            self.assertEqual(result['tables'][name]['status'], 'refused')
            self.assertIn('grouping', result['tables'][name]['reason'])
        # D and E fall below the headcount minimum and stay suppressed.
        self.assertEqual(result['suppressed'], [['D', 'E']])
        # The cumulative release matrix holds only the ABC row and isolates nobody.
        self.assertEqual(result['matrix']['rows'], [[1, 1, 1, 0, 0]])
        self.assertEqual(result['isolable'], [])
        self.assertEqual(result['bounds']['A']['vector_bound'], 50)
        self.assertEqual(result['bounds']['D']['vector_bound'], 0)
        # The exact protocol spends no privacy budget.
        self.assertEqual(result['budget']['spent'], 0.0)
        log = release_access_log(self.db, 'coop-1')
        self.assertEqual([r['outcome'] for r in log],
                         ['published', 'refused', 'refused', 'refused', 'refused'])

    def test_bounded_budget_protocol(self):
        set_budget(self.db, 'coop-1', FIXTURE['budget']['total'])
        cost = FIXTURE['budget']['cost']
        result = release_reports(self.db, SUBJECT, REQUESTS, protocol='bounded-budget',
                                 budget_cost=cost, cap=FIXTURE['cap'])
        # All five tables are published as one noisy joint vector.
        for name in TABLES:
            table = result['tables'][name]
            self.assertEqual(table['status'], 'published')
            self.assertEqual(table['payload']['holders'], 3)
            self.assertTrue(table['payload']['noisy'])
            self.assertEqual(table['payload']['epsilon'], cost)
            self.assertEqual(table['payload']['clip'], [0, 50])
            self.assertNotEqual(table['payload']['animal_hours'], EXACT_TOTALS[name])
        # One unified budget was spent for the whole vector, not five budgets.
        self.assertEqual(result['budget']['spent'], cost)
        self.assertEqual(result['budget']['remaining'], FIXTURE['budget']['total'] - cost)
        # Per-herder contribution upper bounds are public and capped at 3 tables x 50.
        for holder in UNIVERSE:
            self.assertEqual(result['bounds'][holder]['per_table'], 50)
            self.assertEqual(result['bounds'][holder]['vector_bound'], 150)
        # The noisy totals no longer solve to the true individual values.
        noisy = solve(self.vectors(), [result['tables'][n]['payload']['animal_hours'] for n in TABLES])
        self.assertNotEqual([float(v) for v in noisy], [10.0, 20.0, 30.0, 40.0, 50.0])
        # The joint noisy release adds nothing to the exact release matrix.
        self.assertEqual(result['matrix']['rows'], [])
        self.assertEqual(result['isolable'], [])
        # Repeated requests reuse the cached noise sample and spend nothing more.
        again = release_reports(self.db, SUBJECT, REQUESTS, protocol='bounded-budget',
                                budget_cost=cost, cap=FIXTURE['cap'])
        for name in TABLES:
            self.assertEqual(again['tables'][name]['status'], 'cached')
            self.assertEqual(again['tables'][name]['payload'], result['tables'][name]['payload'])
        self.assertEqual(budget_account(self.db, 'coop-1')['spent'], cost)
        log = release_access_log(self.db, 'coop-1')
        self.assertEqual(len([r for r in log if r['outcome'] == 'published']), 5)
        self.assertEqual(len([r for r in log if r['outcome'] == 'cached']), 5)
        # One shared noise seed for the joint vector is kept for audit.
        seeds = {r['noise_seed'] for r in public_releases(self.db, 'coop-1')}
        self.assertEqual(len(seeds), 1)

    def test_correction_keeps_old_public_version(self):
        first = release_reports(self.db, SUBJECT, REQUESTS, protocol='fixed-groups',
                                grouping=FIXTURE['grouping'], minimum=3)
        self.assertEqual(first['tables']['ABC']['payload']['animal_hours'], 60)
        # A late correction: B's herd actually occupied 25 animal-hours.
        with self.db:
            ingest(self.db, correction('pub-move-B2', 'B', 'herd-B', 25, 200))
        second = release_reports(self.db, SUBJECT, REQUESTS, protocol='fixed-groups',
                                 grouping=FIXTURE['grouping'], minimum=3)
        self.assertEqual(second['tables']['ABC']['status'], 'published')
        self.assertEqual(second['tables']['ABC']['version'], 2)
        self.assertEqual(second['tables']['ABC']['payload']['animal_hours'], 65)
        # Both public versions stay in the disclosure ledger.
        versions = [r for r in public_releases(self.db, 'coop-1') if r['table_name'] == 'ABC']
        self.assertEqual([(r['version'], r['payload']['animal_hours']) for r in versions],
                         [(1, 60), (2, 65)])
        # The corrected figure names every original it derives from.
        self.assertEqual(set(versions[1]['evidence_ids']),
                         {'pub-move-A', 'pub-move-B', 'pub-move-B2', 'pub-move-C'})
        # The originals remain internally auditable with exact per-holder pressure.
        moves = [e for e in evidence(self.db, SUBJECT) if e['kind'] == 'movement']
        self.assertEqual(len(moves), 6)
        by_holder, _ = pressure(moves)
        self.assertEqual(by_holder['B'], 25.0)
        self.assertEqual(sum(by_holder.values()), 155.0)

    def test_bounded_budget_correction_spends_then_exhausts(self):
        set_budget(self.db, 'coop-1', FIXTURE['budget']['total'])
        cost = FIXTURE['budget']['cost']
        release_reports(self.db, SUBJECT, REQUESTS, protocol='bounded-budget',
                        budget_cost=cost, cap=FIXTURE['cap'])
        # A correction creates a new data version: a fresh noisy version may
        # be released, but it spends the remaining budget.
        with self.db:
            ingest(self.db, correction('pub-move-B2', 'B', 'herd-B', 25, 200))
        second = release_reports(self.db, SUBJECT, REQUESTS, protocol='bounded-budget',
                                 budget_cost=cost, cap=FIXTURE['cap'])
        self.assertEqual(second['tables']['ABC']['status'], 'published')
        self.assertEqual(second['tables']['ABC']['version'], 2)
        self.assertEqual(second['budget']['spent'], 2.0)
        self.assertEqual(second['budget']['remaining'], 0.0)
        versions = [r for r in public_releases(self.db, 'coop-1') if r['table_name'] == 'ABC']
        self.assertEqual([r['version'] for r in versions], [1, 2])
        self.assertNotEqual(versions[0]['payload'], versions[1]['payload'])
        # A further correction cannot draw new noise: the budget is exhausted,
        # so readers can never average unlimited fresh samples towards the truth.
        with self.db:
            ingest(self.db, correction('pub-move-B3', 'B', 'herd-B', 30, 300))
        third = release_reports(self.db, SUBJECT, REQUESTS, protocol='bounded-budget',
                                budget_cost=cost, cap=FIXTURE['cap'])
        for name in TABLES:
            self.assertEqual(third['tables'][name]['status'], 'refused')
            self.assertIn('budget', third['tables'][name]['reason'])
        self.assertEqual(budget_account(self.db, 'coop-1')['spent'], 2.0)
        self.assertEqual(len(public_releases(self.db, 'coop-1')), 10)
        log = release_access_log(self.db, 'coop-1')
        self.assertEqual(len([r for r in log if r['outcome'] == 'refused']), 5)


if __name__ == '__main__':
    unittest.main()
