from pathlib import Path
COMMAND='movement'
KINDS=['herd', 'movement', 'quarantine']
FAILURE='fixtures/move_overlap.json'
NORMAL=str(Path(__file__).resolve().parent.parent / 'fixtures/move_ok.json')

def allof(events,kind): return [e for e in events if e["kind"]==kind]
def latest(events,kind):
    rows=allof(events,kind)
    return max(rows,key=lambda e:(e["event_time"],e["received_at"],e["id"])) if rows else None

def decide(ev):
    result=[]
    now=max([e["event_time"] for e in ev],default=0)
    def out(name,value,proof):
        sources=[x["id"] for x in proof if x]
        result.append((name,value,sources if sources else [x["id"] for x in ev]))
    permit=latest(ev,'permit')
    now=max([x['event_time'] for x in ev],default=0)
    allowed=bool(permit and permit['data'].get('start',0)<=now<=permit['data'].get('end',10**12))
    allocs=allof(ev,'allocation')
    quota=float(permit['data'].get('quota',0)) if permit else 0
    transferred=sum(float(x['data'].get('amount',0)) for x in allocs)
    samples=allof(ev,'vegetation_sample')
    grass=min([float(x['data'].get('grass',1)) for x in samples],default=1)
    capacity=round(quota*grass,2)
    herds=allof(ev,'herd'); tags=[tag for x in herds for tag in x['data'].get('tags',[])]; out('耳标群身份合并',len(set(tags)),herds)
    moves=allof(ev,'movement'); days=sum(max(0,float(x['data'].get('end',now))-float(x['data'].get('start',now))) *float(x['data'].get('count',0)) for x in moves); out('跨区占用分配',days,moves+herds)
    q=latest(ev,'quarantine'); safe=not q or not q['data'].get('isolated'); out('防疫隔离通行','允许' if safe else '隔离',[q]+moves)
    breaches=[x for x in moves if x['data'].get('outside')]; out('夜间越界巡护',len(breaches),breaches)
    rest=latest(ev,'rest_order')
    open_state=grass>=0.5 and days<=capacity and safe and not (rest and rest['data'].get('active'))
    excess=max(0,days-capacity)
    cleared=all(x['data'].get('end') is not None for x in moves)
    return result
