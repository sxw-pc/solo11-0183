ENTITIES=['pasture', 'permit', 'allocation', 'herd', 'movement', 'quarantine', 'vegetation_sample', 'rest_order', 'compensation']
import json, sqlite3
from pathlib import Path

def connect(path):
    db=sqlite3.connect(path)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL, event_time INTEGER NOT NULL, received_at INTEGER NOT NULL, payload TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS decision (subject TEXT NOT NULL, surface TEXT NOT NULL, version INTEGER NOT NULL, result TEXT NOT NULL, evidence_ids TEXT NOT NULL, PRIMARY KEY(subject,surface,version))')
    db.execute('CREATE TABLE IF NOT EXISTS rejection (id TEXT PRIMARY KEY, reason TEXT NOT NULL, payload TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS public_release (coop TEXT NOT NULL, pasture TEXT NOT NULL, month TEXT NOT NULL, table_name TEXT NOT NULL, version INTEGER NOT NULL, protocol TEXT NOT NULL, members TEXT NOT NULL, payload TEXT NOT NULL, evidence_ids TEXT NOT NULL, data_version TEXT NOT NULL, noise_seed TEXT, budget_spent REAL NOT NULL DEFAULT 0, exact INTEGER NOT NULL, created_at INTEGER NOT NULL, PRIMARY KEY(coop,pasture,month,table_name,version))')
    db.execute('CREATE TABLE IF NOT EXISTS release_access (id INTEGER PRIMARY KEY AUTOINCREMENT, coop TEXT NOT NULL, pasture TEXT NOT NULL, month TEXT NOT NULL, table_name TEXT NOT NULL, version INTEGER, outcome TEXT NOT NULL, detail TEXT NOT NULL, requester TEXT NOT NULL, accessed_at INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS privacy_budget (coop TEXT PRIMARY KEY, total REAL NOT NULL, spent REAL NOT NULL DEFAULT 0)')
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

def _release_row(r):
    return dict(coop=r['coop'],pasture=r['pasture'],month=r['month'],table_name=r['table_name'],version=r['version'],protocol=r['protocol'],members=json.loads(r['members']),payload=json.loads(r['payload']),evidence_ids=json.loads(r['evidence_ids']),data_version=r['data_version'],noise_seed=r['noise_seed'],budget_spent=r['budget_spent'],exact=bool(r['exact']),created_at=r['created_at'])

def save_public_release(db,key,version,protocol,members,payload,evidence_ids,data_version,noise_seed,budget_spent,exact,created_at):
    coop,pasture,month,table=key
    db.execute('INSERT INTO public_release VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(coop,pasture,month,table,int(version),protocol,json.dumps(sorted(members),ensure_ascii=False),json.dumps(payload,ensure_ascii=False,sort_keys=True),json.dumps(sorted(set(evidence_ids)),ensure_ascii=False),data_version,noise_seed,float(budget_spent),1 if exact else 0,int(created_at)))

def public_releases(db,coop=None,exact=None):
    sql='SELECT * FROM public_release';clauses=[];params=[]
    if coop is not None: clauses.append('coop=?');params.append(coop)
    if exact is not None: clauses.append('exact=?');params.append(1 if exact else 0)
    if clauses: sql+=' WHERE '+' AND '.join(clauses)
    sql+=' ORDER BY coop,pasture,month,table_name,version'
    return [_release_row(r) for r in db.execute(sql,params)]

def latest_public_release(db,key):
    row=db.execute('SELECT * FROM public_release WHERE coop=? AND pasture=? AND month=? AND table_name=? ORDER BY version DESC LIMIT 1',tuple(key)).fetchone()
    return _release_row(row) if row else None

def log_release_access(db,key,version,outcome,detail,requester,accessed_at):
    coop,pasture,month,table=key
    db.execute('INSERT INTO release_access (coop,pasture,month,table_name,version,outcome,detail,requester,accessed_at) VALUES (?,?,?,?,?,?,?,?,?)',(coop,pasture,month,table,version,outcome,detail,requester,int(accessed_at)))

def release_access_log(db,coop=None):
    if coop is None:
        rows=db.execute('SELECT * FROM release_access ORDER BY id').fetchall()
    else:
        rows=db.execute('SELECT * FROM release_access WHERE coop=? ORDER BY id',(coop,)).fetchall()
    return [dict(r) for r in rows]

def set_budget(db,coop,total):
    db.execute('INSERT INTO privacy_budget (coop,total,spent) VALUES (?,?,0) ON CONFLICT(coop) DO UPDATE SET total=excluded.total',(coop,float(total)))

def budget_account(db,coop):
    row=db.execute('SELECT total,spent FROM privacy_budget WHERE coop=?',(coop,)).fetchone()
    if row is None: return {'total':0.0,'spent':0.0,'remaining':0.0}
    return {'total':row['total'],'spent':row['spent'],'remaining':round(row['total']-row['spent'],9)}

def spend_budget(db,coop,amount):
    account=budget_account(db,coop)
    if account['remaining']+1e-9<amount: raise ValueError('privacy budget exhausted for '+coop)
    db.execute('UPDATE privacy_budget SET spent=spent+? WHERE coop=?',(float(amount),coop))
    return budget_account(db,coop)
