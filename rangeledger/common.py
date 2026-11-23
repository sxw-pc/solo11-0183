from pathlib import Path
from collections import defaultdict
def rows(ev,kind):return [e for e in ev if e['kind']==kind]
def latest(ev,kind):
    values=rows(ev,kind)
    return max(values,key=lambda e:(e['event_time'],e['received_at'],e['id'])) if values else None
def proof(*groups):return [e['id'] for group in groups for e in group if e]
def night_hours(a,b):
    total=0
    for day in range(int(a//24),int(b//24)+1):
        for x,y in ((day*24,day*24+6),(day*24+20,day*24+24)):
            total+=max(0,min(b,y)-max(a,x))
    return total
def pressure(ev):
    groups=defaultdict(list)
    for m in rows(ev,'movement'):
        d=m['data'];a=float(d.get('start',0));b=float(d.get('end',a));count=float(d.get('count',0))
        if b<a or count<0:continue
        groups[(d.get('zone','P1'),d.get('herd','herd-1'),d.get('holder','H1'))].append((a,b,count))
    by_holder=defaultdict(float);by_zone=defaultdict(float)
    for (zone,herd,holder),segments in groups.items():
        points=sorted({t for a,b,_ in segments for t in (a,b)})
        for a,b in zip(points,points[1:]):
            weight=max((c for x,y,c in segments if x<b and y>a),default=0)
            value=(b-a)*weight;by_holder[holder]+=value;by_zone[zone]+=value
    return dict(by_holder),dict(by_zone)
def capacity(ev):
    permit=latest(ev,'permit');samples=rows(ev,'vegetation_sample')
    base=float(permit['data'].get('quota',0)) if permit else 0
    grass=min((float(s['data'].get('grass',1)) for s in samples),default=1)
    return round(base*grass,2),grass
def transfer_balances(ev):
    permit=latest(ev,'permit');owner=permit['data'].get('holder','H1') if permit else 'H1'
    balances={owner:float(permit['data'].get('quota',0)) if permit else 0}
    valid=True
    for event in sorted(rows(ev,'allocation'),key=lambda x:(x['event_time'],x['id'])):
        d=event['data'];source=d.get('from',owner);dest=d.get('to','H2');amount=float(d.get('amount',0))
        if amount<0 or balances.get(source,0)<amount or source==dest:valid=False;continue
        balances[source]-=amount;balances[dest]=balances.get(dest,0)+amount
    return balances,valid
