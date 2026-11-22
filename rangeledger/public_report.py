from .cohort_stats import aggregate

def release(rows, minimum=3):
    result=aggregate(rows)
    return result if result['holders']>=minimum else {'suppressed':True}
