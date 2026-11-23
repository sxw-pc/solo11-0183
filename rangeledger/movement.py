from .common import rows,latest,proof,pressure,night_hours
from .track import analyze
from pathlib import Path
COMMAND='movement';KINDS=['herd','movement','quarantine']
NORMAL=str(Path(__file__).resolve().parent.parent/'fixtures/move_ok.json')
FAILURE='fixtures/move_overlap.json'
def decide(ev):
    herds=rows(ev,'herd');moves=rows(ev,'movement');q=latest(ev,'quarantine')
    tags={tag for h in herds for tag in h['data'].get('tags',[])}
    _,zones=pressure(ev)
    safe=not q or not q['data'].get('isolated')
    analysis=analyze(ev)
    tracked=analysis['tracked'] if analysis else set()
    boolean=[m for m in moves if m['data'].get('outside') and m['id'] not in tracked]
    breaches=round(sum(night_hours(float(m['data'].get('start',0)),float(m['data'].get('end',0))) for m in boolean)+(analysis['night_total'] if analysis else 0),6)
    ids=sorted(set(proof(boolean))|set(analysis['evidence'] if analysis else [])) or proof(ev)
    return [('耳标群身份合并',len(tags),proof(herds) or proof(ev)),('跨区占用分配',zones,proof(moves,herds) or proof(ev)),('防疫隔离通行','允许' if safe else '隔离',proof([q],moves) or proof(ev)),('夜间越界巡护',breaches,ids)]
