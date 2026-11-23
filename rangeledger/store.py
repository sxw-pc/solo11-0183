ENTITIES=['pasture', 'permit', 'allocation', 'herd', 'movement', 'quarantine', 'vegetation_sample', 'rest_order', 'compensation', 'pedigree', 'parentage_review']
import json, sqlite3
from pathlib import Path

from .breeding import serialize_result


def _json_default(obj):
    # Fraction 已由 serialize_result 预处理，这里仅兜底其它可序列化对象
    if hasattr(obj, 'isoformat'):
        return obj.isoformat()
    raise TypeError(f'不可落库的对象类型 {type(obj)!r}')


def connect(path):
    db=sqlite3.connect(path)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL, event_time INTEGER NOT NULL, received_at INTEGER NOT NULL, payload TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS decision (subject TEXT NOT NULL, surface TEXT NOT NULL, version INTEGER NOT NULL, result TEXT NOT NULL, evidence_ids TEXT NOT NULL, PRIMARY KEY(subject,surface,version))')
    db.execute('CREATE TABLE IF NOT EXISTS rejection (id TEXT PRIMARY KEY, reason TEXT NOT NULL, payload TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS pedigree_snapshot (subject TEXT NOT NULL, version INTEGER NOT NULL, snapshot_id TEXT NOT NULL, parent_snapshot_id TEXT, rationale TEXT NOT NULL, event_time INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(subject,version))')
    db.execute('CREATE TABLE IF NOT EXISTS breeding_decision (subject TEXT NOT NULL, pair_id TEXT NOT NULL, version INTEGER NOT NULL, result TEXT NOT NULL, evidence_ids TEXT NOT NULL, PRIMARY KEY(subject,pair_id,version))')
    for kind in ENTITIES:
        db.execute(f'CREATE TABLE IF NOT EXISTS {kind} (id TEXT PRIMARY KEY REFERENCES evidence(id), subject TEXT NOT NULL, event_time INTEGER NOT NULL, payload TEXT NOT NULL)')
    db.commit()
    return db

def ingest(db, event):
    for key in ('id','kind','subject','event_time','received_at'):
        if key not in event: raise ValueError('missing '+key)
    if event['kind'] not in ENTITIES: raise ValueError('unknown kind')
    payload=json.dumps(event.get('data',{}),ensure_ascii=False,sort_keys=True)
    old=db.execute('SELECT kind,subject,event_time,received_at,payload FROM evidence WHERE id=?',(event['id'],)).fetchone()
    current=(event['kind'],event['subject'],int(event['event_time']),int(event['received_at']),payload)
    if old:
        if tuple(old)==current: return False
        raise ValueError('conflicting evidence id '+event['id'])
    db.execute('INSERT INTO evidence VALUES (?,?,?,?,?,?)',(event['id'],*current))
    db.execute(f'INSERT INTO {event["kind"]} VALUES (?,?,?,?)',(event['id'],event['subject'],int(event['event_time']),payload))
    return True

def evidence(db,subject):
    return [dict(id=r['id'],kind=r['kind'],subject=r['subject'],event_time=r['event_time'],received_at=r['received_at'],data=json.loads(r['payload'])) for r in db.execute('SELECT * FROM evidence WHERE subject=? ORDER BY event_time,id',(subject,))]

def save_decisions(db,subject,results):
    for surface,result,ids in results:
        value=json.dumps(result,ensure_ascii=False,sort_keys=True)
        proof=json.dumps(sorted(set(ids)),ensure_ascii=False)
        old=db.execute('SELECT version,result,evidence_ids FROM decision WHERE subject=? AND surface=? ORDER BY version DESC LIMIT 1',(subject,surface)).fetchone()
        if old and old['result']==value and old['evidence_ids']==proof: continue
        version=1 if old is None else old['version']+1
        db.execute('INSERT INTO decision VALUES (?,?,?,?,?)',(subject,surface,version,value,proof))

def report(db,subject):
    rows=db.execute('SELECT d.* FROM decision d JOIN (SELECT surface,MAX(version) v FROM decision WHERE subject=? GROUP BY surface) x ON d.surface=x.surface AND d.version=x.v WHERE d.subject=? ORDER BY d.surface',(subject,subject)).fetchall()
    return {r['surface']:dict(version=r['version'],result=json.loads(r['result']),evidence_ids=json.loads(r['evidence_ids'])) for r in rows}

def save_pedigree_snapshot(db,subject,snapshot,event_time):
    """保存一版祖谱 DAG 快照。同 (subject,version) 重复内容幂等，冲突报错。"""
    payload=json.dumps(snapshot,ensure_ascii=False,sort_keys=True,default=_json_default)
    old=db.execute('SELECT payload FROM pedigree_snapshot WHERE subject=? AND version=?',(subject,snapshot['version'])).fetchone()
    if old:
        if old['payload']==payload: return snapshot['version']
        raise ValueError(f"祖谱快照 {subject} v{snapshot['version']} 内容冲突，版本不可覆写")
    db.execute('INSERT INTO pedigree_snapshot VALUES (?,?,?,?,?,?,?)',(
        subject,snapshot['version'],snapshot['snapshot_id'],
        snapshot.get('parent_snapshot_id'),snapshot.get('rationale',''),
        int(event_time),payload))
    return snapshot['version']

def pedigree_versions(db,subject):
    """按版本号返回全部祖谱快照，旧版永远可追溯。"""
    rows=db.execute('SELECT * FROM pedigree_snapshot WHERE subject=? ORDER BY version',(subject,)).fetchall()
    return [dict(version=r['version'],snapshot_id=r['snapshot_id'],
                 parent_snapshot_id=r['parent_snapshot_id'],
                 rationale=r['rationale'],event_time=r['event_time'],
                 snapshot=json.loads(r['payload'])) for r in rows]

def latest_pedigree_snapshot(db,subject):
    rows=pedigree_versions(db,subject)
    return rows[-1] if rows else None

def save_breeding_decision(db,subject,result):
    """保存一条配种签发/拒签决定；内容与证据完全相同则幂等跳过。"""
    pair_id='x'.join(result['pair'])
    value=json.dumps(serialize_result(result),ensure_ascii=False,sort_keys=True,default=_json_default)
    proof=json.dumps(sorted(set(result.get('evidence_ids',[]))),ensure_ascii=False)
    old=db.execute('SELECT version,result,evidence_ids FROM breeding_decision WHERE subject=? AND pair_id=? ORDER BY version DESC LIMIT 1',(subject,pair_id)).fetchone()
    if old and old['result']==value and old['evidence_ids']==proof:
        return old['version']
    version=1 if old is None else old['version']+1
    db.execute('INSERT INTO breeding_decision VALUES (?,?,?,?,?)',(subject,pair_id,version,value,proof))
    return version

def breeding_history(db,subject,pair_id=None):
    """返回配种决定的完整版本链（含被更正的旧签发计算）。"""
    if pair_id is None:
        rows=db.execute('SELECT * FROM breeding_decision WHERE subject=? ORDER BY pair_id,version',(subject,)).fetchall()
    else:
        rows=db.execute('SELECT * FROM breeding_decision WHERE subject=? AND pair_id=? ORDER BY version',(subject,pair_id)).fetchall()
    return [dict(pair_id=r['pair_id'],version=r['version'],
                 result=json.loads(r['result']),
                 evidence_ids=json.loads(r['evidence_ids'])) for r in rows]
