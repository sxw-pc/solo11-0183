ENTITIES=['pasture', 'permit', 'allocation', 'herd', 'movement', 'quarantine', 'vegetation_sample', 'rest_order', 'compensation', 'fence_topology', 'fence_alarm', 'fence_retest']
import json, sqlite3
from pathlib import Path

def connect(path):
    db=sqlite3.connect(path)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL, event_time INTEGER NOT NULL, received_at INTEGER NOT NULL, payload TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS decision (subject TEXT NOT NULL, surface TEXT NOT NULL, version INTEGER NOT NULL, result TEXT NOT NULL, evidence_ids TEXT NOT NULL, PRIMARY KEY(subject,surface,version))')
    db.execute('CREATE TABLE IF NOT EXISTS rejection (id TEXT PRIMARY KEY, reason TEXT NOT NULL, payload TEXT NOT NULL)')
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
