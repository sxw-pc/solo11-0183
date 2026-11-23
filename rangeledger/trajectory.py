from .common import proof
from .track import analyze
from pathlib import Path
COMMAND='trajectory';KINDS=['gps_package','fence','transit_permit','movement','permit']
NORMAL=str(Path(__file__).resolve().parent.parent/'fixtures/track_ok.json')
FAILURE='fixtures/track_tampered.json'
def decide(ev):
    analysis=analyze(ev)
    if analysis is None: return []
    ids=analysis['evidence'] or proof(ev)
    return [('轨迹采录核验',analysis['ingestion'],ids),('越界片段明细',analysis['table'],ids),('许可叠置覆盖',analysis['permits'],ids)]
