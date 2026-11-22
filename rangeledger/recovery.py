from .common import rows,latest,proof,pressure,capacity,night_hours
from pathlib import Path
COMMAND='recovery';KINDS=['vegetation_sample','rest_order','compensation']
NORMAL=str(Path(__file__).resolve().parent.parent/'fixtures/recover_ok.json')
FAILURE='fixtures/recover_late.json'
def decide(ev):
    holder_days,zones=pressure(ev);cap,grass=capacity(ev)
    q=latest(ev,'quarantine');safe=not q or not q['data'].get('isolated')
    rest=latest(ev,'rest_order');orders=rows(ev,'rest_order');moves=rows(ev,'movement')
    previous_active=max((o for o in orders if o['data'].get('active')),key=lambda x:x['event_time'],default=None)
    rechecked=not previous_active or any(s['event_time']>previous_active['event_time'] for s in rows(ev,'vegetation_sample'))
    total=sum(zones.values());open_state=grass>=0.5 and total<=cap and safe and not (rest and rest['data'].get('active')) and rechecked
    breach_by_holder={}
    for m in moves:
        if m['data'].get('outside'):
            holder=m['data'].get('holder','H1');breach_by_holder[holder]=breach_by_holder.get(holder,0)+night_hours(float(m['data'].get('start',0)),float(m['data'].get('end',0)))
    excess=max(0,total-cap);comp={}
    for holder,days in holder_days.items():
        comp[holder]=round((excess*days/total if total else 0)*10+breach_by_holder.get(holder,0)*5,2)
    clear=all(m['data'].get('end') is not None and (not rest or float(m['data']['end'])<=float(rest['data'].get('deadline',10**12))) for m in moves)
    state='重开' if open_state and clear else '清场中' if rest and rest['data'].get('active') else '待复核'
    ids=proof(rows(ev,'vegetation_sample'),moves,[rest,q]) or proof(ev)
    return [('植被恢复阈值判定','开放' if open_state else '休牧',ids),('超载补偿分摊',comp,ids),('休牧令执行与重开',state,ids)]
