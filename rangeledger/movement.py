from .common import rows,latest,proof,pressure,night_hours
from pathlib import Path
COMMAND='movement';KINDS=['herd','movement','quarantine']
NORMAL=str(Path(__file__).resolve().parent.parent/'fixtures/move_ok.json')
FAILURE='fixtures/move_overlap.json'
def decide(ev):
    herds=rows(ev,'herd');moves=rows(ev,'movement');q=latest(ev,'quarantine')
    tags={tag for h in herds for tag in h['data'].get('tags',[])}
    _,zones=pressure(ev)
    safe=not q or not q['data'].get('isolated')
    breaches=sum(night_hours(float(m['data'].get('start',0)),float(m['data'].get('end',0))) for m in moves if m['data'].get('outside'))
    return [('耳标群身份合并',len(tags),proof(herds) or proof(ev)),('跨区占用分配',zones,proof(moves,herds) or proof(ev)),('防疫隔离通行','允许' if safe else '隔离',proof([q],moves) or proof(ev)),('夜间越界巡护',breaches,proof([m for m in moves if m['data'].get('outside')]) or proof(ev))]
