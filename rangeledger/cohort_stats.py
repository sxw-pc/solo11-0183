"""Cohort aggregation and joint-disclosure review helpers.

The cooperative publishes anonymised pasture-pressure statistics.  A single
table may pass the headcount rule yet still, together with older public
tables, let a reader solve a linear system for one herder's exact
animal-hours.  These helpers build the cumulative release matrix and check
whether any herder's contribution becomes linearly isolable, using exact
rational arithmetic so the review never depends on float tolerance.
"""
from fractions import Fraction


def aggregate(rows):
    return {'holders': len(rows), 'animal_hours': sum(row['animal_hours'] for row in rows)}


def member_vector(members, universe):
    """0/1 membership of ``members`` over the ordered ``universe``."""
    chosen = set(members)
    return [1 if holder in chosen else 0 for holder in universe]


def release_matrix(vectors):
    """Cumulative release matrix: one exact row per published member vector."""
    return [[Fraction(v) for v in vector] for vector in vectors]


def _rank(matrix):
    """Exact row rank over the rationals (Gauss elimination)."""
    rows = [list(map(Fraction, row)) for row in matrix]
    if not rows:
        return 0
    width = len(rows[0])
    rank = 0
    for col in range(width):
        pivot = next((i for i in range(rank, len(rows)) if rows[i][col] != 0), None)
        if pivot is None:
            continue
        rows[rank], rows[pivot] = rows[pivot], rows[rank]
        head = rows[rank]
        for i in range(len(rows)):
            if i != rank and rows[i][col] != 0:
                factor = rows[i][col] / head[col]
                rows[i] = [value - factor * keep for value, keep in zip(rows[i], head)]
        rank += 1
        if rank == len(rows):
            break
    return rank


def isolable_holders(matrix, universe):
    """Holders whose exact contribution is a linear combination of the rows.

    A herder's unit vector lying in the row space of the release matrix
    means the public totals determine that herder's animal-hours exactly.
    """
    if not matrix:
        return set()
    base = _rank(matrix)
    exposed = set()
    for i, holder in enumerate(universe):
        unit = [Fraction(0)] * len(universe)
        unit[i] = Fraction(1)
        if _rank(list(matrix) + [unit]) == base:
            exposed.add(holder)
    return exposed


def joint_review(published_vectors, new_vector, universe):
    """Review a candidate table jointly with every already-public table.

    ``published_vectors`` are the member vectors of all exact tables released
    so far; the candidate is never judged by its own headcount alone.
    """
    matrix = release_matrix(list(published_vectors) + [list(new_vector)])
    exposed = isolable_holders(matrix, universe)
    return {
        'allowed': not exposed,
        'isolable': sorted(exposed),
        'rank': _rank(matrix),
        'tables': len(matrix),
    }


def clip_contributions(rows, low, high):
    """Clip each row's animal-hours into ``[low, high]`` (contribution bounding)."""
    clipped = []
    for row in rows:
        value = max(low, min(high, row['animal_hours']))
        clipped.append({**row, 'animal_hours': value})
    return clipped


def contribution_bounds(universe, vectors, cap):
    """Public per-herder contribution upper bounds.

    A herder adds at most ``cap`` animal-hours to one table, so their
    contribution to the joint vector of ``vectors`` is bounded by
    ``cap`` times the number of tables they belong to.  The bounds depend
    only on the public membership matrix and the public cap.
    """
    bounds = {holder: {'tables': 0, 'per_table': cap, 'vector_bound': 0} for holder in universe}
    for vector in vectors:
        for holder, flag in zip(universe, vector):
            if flag:
                bounds[holder]['tables'] += 1
    for holder in universe:
        bounds[holder]['vector_bound'] = cap * bounds[holder]['tables']
    return bounds


def vector_sensitivity(universe, vectors, cap):
    """L1 sensitivity of the joint vector query under the clip cap."""
    bounds = contribution_bounds(universe, vectors, cap)
    return max((bound['vector_bound'] for bound in bounds.values()), default=0)
