"""Public release of anonymised pasture-pressure cross tables.

Every report is keyed by (pasture, month, cooperative) and released through
``release_reports``.  A candidate table is always reviewed jointly with the
cumulative release matrix of every exact table published before -- never
against its own headcount alone -- so readers cannot line up several
count-qualified tables and solve for one herder.

Two authorised protocols are available to the publisher:

* ``fixed-groups`` -- exact sums for a declared non-crossing grouping only;
  groups below the minimum headcount are suppressed and ad-hoc cross tables
  that could jointly isolate a herder are refused.
* ``bounded-budget`` -- the requested tables are released as one joint
  vector: each herder's hours are first clipped to [0, cap], a single
  privacy budget is spent for the whole vector, and controlled noise is
  added once per data version.  Repeated requests are served from the
  disclosure cache and never draw fresh noise.

Correcting a movement produces a new data version: the corrected report is
published as a new version while every older public version stays in the
disclosure ledger, and the original movement records remain in the evidence
store for internal audit.
"""
import hashlib
import json
import math
import random

from .cohort_stats import (
    aggregate,
    clip_contributions,
    contribution_bounds,
    isolable_holders,
    joint_review,
    member_vector,
    release_matrix,
    vector_sensitivity,
)
from .common import pressure
from . import store

FIXED_GROUPS = 'fixed-groups'
BOUNDED_BUDGET = 'bounded-budget'
PROTOCOLS = (FIXED_GROUPS, BOUNDED_BUDGET)
CLIP_LOW = 0.0
CLIP_HIGH = 50.0


def release(rows, minimum=3):
    result = aggregate(rows)
    return result if result['holders'] >= minimum else {'suppressed': True}


def _num(value):
    value = float(value)
    return int(value) if value.is_integer() else round(value, 6)


def _cell_movements(records, pasture, month, coop):
    cell = []
    for event in records:
        if event['kind'] != 'movement':
            continue
        data = event['data']
        if data.get('zone') == pasture and data.get('month') == month and data.get('coop') == coop:
            cell.append(event)
    return cell


def _cohort(records, pasture, month, coop):
    """Exact per-holder occupancy of one cell plus the originals behind it."""
    cell = _cell_movements(records, pasture, month, coop)
    by_holder, _ = pressure(cell)
    provenance = {}
    for event in sorted(cell, key=lambda e: (e['event_time'], e['received_at'], e['id'])):
        provenance.setdefault(event['data'].get('holder', 'H1'), []).append(event['id'])
    material = json.dumps(
        [(e['id'], e['event_time'], e['received_at'], e['data']) for e in sorted(cell, key=lambda e: e['id'])],
        ensure_ascii=False, sort_keys=True)
    data_version = hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]
    return by_holder, provenance, data_version


def _laplace(rng, scale):
    u = rng.random() - 0.5
    if u == 0.0:
        return 0.0
    return -scale * math.copysign(1.0, u) * math.log(1.0 - 2.0 * abs(u))


def _noise_seed(coop, names, data_versions, cost):
    material = json.dumps(
        {'coop': coop, 'tables': sorted(names), 'data_versions': sorted(data_versions), 'epsilon': cost},
        ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


def _key(request):
    return (request['coop'], request['pasture'], request['month'], request['table'])


def _ledger_vectors(db, coop, universe):
    """Member vectors of every exact table this coop has ever published."""
    rows = store.public_releases(db, coop=coop, exact=True)
    return [member_vector(row['members'], universe) for row in rows]


def _deliverable(db, subject, universe, coop, protocol, tables, suppressed, vectors, cap):
    """Assemble the public deliverable: reports, sources, per-herder
    contribution bounds, member vectors and the cumulative release matrix."""
    matrix = release_matrix(_ledger_vectors(db, coop, universe))
    return {
        'protocol': protocol,
        'subject': subject,
        'universe': list(universe),
        'tables': tables,
        'suppressed': suppressed,
        'bounds': contribution_bounds(universe, vectors, cap),
        'matrix': {'universe': list(universe), 'rows': [[int(v) for v in row] for row in matrix]},
        'isolable': sorted(isolable_holders(matrix, universe)),
        'budget': store.budget_account(db, coop),
    }


def _release_fixed_groups(db, subject, requests, records, universe, grouping, minimum, cap, requester, now):
    groups = [sorted(set(group)) for group in (grouping or [])]
    suppressed = [group for group in groups if len(group) < minimum]
    tables = {}
    batch_vectors = {request['coop']: [] for request in requests}
    with db:
        for request in requests:
            key = _key(request)
            coop = request['coop']
            members = sorted(set(request['members']))
            vector = member_vector(members, universe)
            by_holder, provenance, data_version = _cohort(records, request['pasture'], request['month'], coop)
            latest = store.latest_public_release(db, key)
            if latest and latest['protocol'] == FIXED_GROUPS and latest['data_version'] == data_version:
                store.log_release_access(db, key, latest['version'], 'cached', 'served from disclosure cache', requester, now)
                tables[request['table']] = dict(status='cached', version=latest['version'], members=latest['members'],
                                                member_vector=member_vector(latest['members'], universe),
                                                payload=latest['payload'], evidence_ids=latest['evidence_ids'])
                if latest['exact']:
                    batch_vectors[coop].append(member_vector(latest['members'], universe))
                continue
            if members not in groups:
                store.log_release_access(db, key, None, 'refused', 'outside fixed grouping', requester, now)
                tables[request['table']] = dict(status='refused', reason='outside fixed grouping',
                                                members=members, member_vector=vector)
                continue
            if len(members) < minimum:
                store.log_release_access(db, key, None, 'suppressed', f'holders {len(members)} < {minimum}', requester, now)
                tables[request['table']] = dict(status='suppressed', reason=f'holders {len(members)} < {minimum}',
                                                members=members, member_vector=vector)
                continue
            missing = [holder for holder in members if holder not in by_holder]
            if missing:
                store.log_release_access(db, key, None, 'refused', 'missing source: ' + ','.join(missing), requester, now)
                tables[request['table']] = dict(status='refused', reason='missing source: ' + ','.join(missing),
                                                members=members, member_vector=vector)
                continue
            prior = _ledger_vectors(db, coop, universe) + batch_vectors[coop]
            review = joint_review(prior, vector, universe)
            if not review['allowed']:
                reason = 'joint isolation exposes ' + ','.join(review['isolable'])
                store.log_release_access(db, key, None, 'refused', reason, requester, now)
                tables[request['table']] = dict(status='refused', reason=reason, members=members, member_vector=vector)
                continue
            evidence_ids = sorted({eid for holder in members for eid in provenance.get(holder, [])})
            payload = {'holders': len(members), 'animal_hours': _num(sum(by_holder[h] for h in members))}
            version = latest['version'] + 1 if latest else 1
            store.save_public_release(db, key, version, FIXED_GROUPS, members, payload, evidence_ids,
                                      data_version, None, 0.0, True, now)
            store.log_release_access(db, key, version, 'published', 'exact group release', requester, now)
            batch_vectors[coop].append(vector)
            tables[request['table']] = dict(status='published', version=version, members=members, member_vector=vector,
                                            payload=payload, evidence_ids=evidence_ids)
    coop = requests[0]['coop']
    vectors = [t['member_vector'] for t in tables.values() if t.get('status') in ('published', 'cached')]
    return _deliverable(db, subject, universe, coop, FIXED_GROUPS, tables, suppressed, vectors, cap)


def _release_bounded_budget(db, subject, requests, records, universe, minimum, budget_cost, cap, requester, now):
    coop = requests[0]['coop']
    tables = {}
    prepared = []
    for request in requests:
        members = sorted(set(request['members']))
        vector = member_vector(members, universe)
        by_holder, provenance, data_version = _cohort(records, request['pasture'], request['month'], request['coop'])
        prepared.append(dict(request=request, key=_key(request), members=members, vector=vector,
                             by_holder=by_holder, provenance=provenance, data_version=data_version,
                             latest=store.latest_public_release(db, _key(request))))
    cached = all(p['latest'] and p['latest']['protocol'] == BOUNDED_BUDGET
                 and p['latest']['data_version'] == p['data_version'] for p in prepared)
    if cached:
        with db:
            for p in prepared:
                latest = p['latest']
                store.log_release_access(db, p['key'], latest['version'], 'cached', 'served from disclosure cache', requester, now)
                tables[p['request']['table']] = dict(status='cached', version=latest['version'], members=latest['members'],
                                                     member_vector=member_vector(latest['members'], universe),
                                                     payload=latest['payload'], evidence_ids=latest['evidence_ids'])
        vectors = [t['member_vector'] for t in tables.values()]
        return _deliverable(db, subject, universe, coop, BOUNDED_BUDGET, tables, [], vectors, cap)
    valid = []
    with db:
        for p in prepared:
            request = p['request']
            if len(p['members']) < minimum:
                store.log_release_access(db, p['key'], None, 'suppressed',
                                         f"holders {len(p['members'])} < {minimum}", requester, now)
                tables[request['table']] = dict(status='suppressed', reason=f"holders {len(p['members'])} < {minimum}",
                                                members=p['members'], member_vector=p['vector'])
                continue
            missing = [h for h in p['members'] if h not in p['by_holder']]
            if missing:
                store.log_release_access(db, p['key'], None, 'refused', 'missing source: ' + ','.join(missing), requester, now)
                tables[request['table']] = dict(status='refused', reason='missing source: ' + ','.join(missing),
                                                members=p['members'], member_vector=p['vector'])
                continue
            valid.append(p)
    if not valid:
        return _deliverable(db, subject, universe, coop, BOUNDED_BUDGET, tables, [], [], cap)
    account = store.budget_account(db, coop)
    if budget_cost <= 0 or account['remaining'] + 1e-9 < budget_cost:
        with db:
            for p in valid:
                store.log_release_access(db, p['key'], None, 'refused', 'privacy budget exhausted', requester, now)
                tables[p['request']['table']] = dict(status='refused', reason='privacy budget exhausted',
                                                     members=p['members'], member_vector=p['vector'])
        vectors = [t['member_vector'] for t in tables.values() if t.get('status') in ('published', 'cached')]
        return _deliverable(db, subject, universe, coop, BOUNDED_BUDGET, tables, [], vectors, cap)
    vectors = [p['vector'] for p in valid]
    sensitivity = vector_sensitivity(universe, vectors, cap)
    scale = sensitivity / budget_cost
    seed = _noise_seed(coop, [p['request']['table'] for p in valid],
                       [p['data_version'] for p in valid], budget_cost)
    rng = random.Random(int(seed, 16))
    with db:
        store.spend_budget(db, coop, budget_cost)
        for p in sorted(valid, key=lambda p: p['request']['table']):
            request = p['request']
            clipped = clip_contributions(
                [{'holder': h, 'animal_hours': p['by_holder'][h]} for h in p['members']], CLIP_LOW, cap)
            exact_sum = sum(row['animal_hours'] for row in clipped)
            noisy = exact_sum + _laplace(rng, scale)
            payload = {'holders': len(p['members']), 'animal_hours': round(noisy, 2), 'noisy': True,
                       'epsilon': budget_cost, 'clip': [_num(CLIP_LOW), _num(cap)],
                       'sensitivity': _num(sensitivity)}
            evidence_ids = sorted({eid for h in p['members'] for eid in p['provenance'].get(h, [])})
            version = p['latest']['version'] + 1 if p['latest'] else 1
            store.save_public_release(db, p['key'], version, BOUNDED_BUDGET, p['members'], payload, evidence_ids,
                                      p['data_version'], seed, budget_cost / len(valid), False, now)
            store.log_release_access(db, p['key'], version, 'published',
                                     f'joint noisy vector release (epsilon={budget_cost})', requester, now)
            tables[request['table']] = dict(status='published', version=version, members=p['members'],
                                            member_vector=p['vector'], payload=payload, evidence_ids=evidence_ids)
    vectors = [t['member_vector'] for t in tables.values() if t.get('status') in ('published', 'cached')]
    return _deliverable(db, subject, universe, coop, BOUNDED_BUDGET, tables, [], vectors, cap)


def release_reports(db, subject, requests, protocol, grouping=None, minimum=3,
                    budget_cost=0.0, cap=CLIP_HIGH, requester='public', now=0):
    """Release a batch of (pasture, month, coop) cross tables.

    Returns the public deliverable: the releasable report set, the source
    evidence ids behind every figure, the per-herder contribution upper
    bounds, each table's member vector, the cumulative release matrix and
    the remaining privacy budget.  Every request is written to the access
    log inside the same transaction as the release it triggers.
    """
    if protocol not in PROTOCOLS:
        raise ValueError('unknown protocol ' + str(protocol))
    if not requests:
        raise ValueError('no requests')
    records = store.evidence(db, subject)
    universe = sorted({e['data'].get('holder', 'H1') for e in records if e['kind'] == 'movement'})
    if protocol == FIXED_GROUPS:
        return _release_fixed_groups(db, subject, requests, records, universe, grouping, minimum, cap, requester, now)
    return _release_bounded_budget(db, subject, requests, records, universe, minimum, budget_cost, cap, requester, now)
