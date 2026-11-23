from .common import rows,latest,proof,pressure,capacity,transfer_balances
from pathlib import Path
COMMAND='permit';KINDS=['pasture','permit','allocation']
NORMAL=str(Path(__file__).resolve().parent.parent/'fixtures/permit_ok.json')
FAILURE='fixtures/permit_conflict.json'
def decide(ev):
    permit=latest(ev,'permit');moves=rows(ev,'movement');balances,valid=transfer_balances(ev)
    allowed=bool(permit and all(permit['data'].get('start',-10**12)<=m['data'].get('start',0) and m['data'].get('end',0)<=permit['data'].get('end',10**12) for m in moves))
    cap,_=capacity(ev)
    return [('许可期限放行','准入' if allowed else '禁入',proof([permit],moves) or proof(ev)),('份额转让守恒',balances if valid else '超配',proof([permit],rows(ev,'allocation')) or proof(ev)),('旱季草量折减',cap,proof([permit],rows(ev,'vegetation_sample')) or proof(ev))]
