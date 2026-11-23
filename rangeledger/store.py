ENTITIES=['pasture', 'permit', 'allocation', 'herd', 'movement', 'quarantine', 'vegetation_sample', 'rest_order', 'compensation',
          # 离线水管网：原始读数/校准/改表、管道方向、阀门动作、蓄水池水位、漏损归责签认
          'meter_reading', 'calibration', 'meter_replacement', 'pipe', 'valve_action', 'reservoir_level', 'liability_signoff']
import json, sqlite3

def connect(path):
    db=sqlite3.connect(path)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL, event_time INTEGER NOT NULL, received_at INTEGER NOT NULL, payload TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS decision (subject TEXT NOT NULL, surface TEXT NOT NULL, version INTEGER NOT NULL, result TEXT NOT NULL, evidence_ids TEXT NOT NULL, PRIMARY KEY(subject,surface,version))')
    db.execute('CREATE TABLE IF NOT EXISTS rejection (id TEXT PRIMARY KEY, reason TEXT NOT NULL, payload TEXT NOT NULL)')
    # 经签认协议出具的用水凭证：同一物理账的不同归责版本都要留痕，不能只改报表标签
    db.execute('CREATE TABLE IF NOT EXISTS voucher (subject TEXT NOT NULL, protocol TEXT NOT NULL, version INTEGER NOT NULL, result TEXT NOT NULL, signoff_event_id TEXT NOT NULL, PRIMARY KEY(subject,protocol,version))')
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

def save_voucher(db,subject,protocol,result,signoff_event_id):
    """凭证按 (subject,protocol) 独立版本化；内容不变则不新增版本。"""
    value=json.dumps(result,ensure_ascii=False,sort_keys=True)
    old=db.execute('SELECT version,result FROM voucher WHERE subject=? AND protocol=? ORDER BY version DESC LIMIT 1',(subject,protocol)).fetchone()
    if old and old['result']==value: return old['version']
    version=1 if old is None else old['version']+1
    db.execute('INSERT INTO voucher VALUES (?,?,?,?,?)',(subject,protocol,version,value,signoff_event_id))
    return version

def vouchers(db,subject):
    rows=db.execute('SELECT v.* FROM voucher v JOIN (SELECT protocol,MAX(version) mv FROM voucher WHERE subject=? GROUP BY protocol) x ON v.protocol=x.protocol AND v.version=x.mv ORDER BY v.protocol',(subject,)).fetchall()
    return {r['protocol']:dict(version=r['version'],result=json.loads(r['result']),signoff_event_id=r['signoff_event_id']) for r in rows}

def save_node_balances(db,subject,events,reconciliation):
    """节点守恒差额作为版本化决定留存；每条附对应仪表、证据与冻结理由。"""
    meter_readings={}
    for event in events:
        if event.get('kind')=='meter_reading':
            meter_readings.setdefault(event['data'].get('meter_id'),[]).append(event['id'])
    results=[]
    for node,info in reconciliation['nodes'].items():
        proof=set()
        for meter_id in info['meters']:
            proof.update(meter_readings.get(meter_id,()))
        if info['reasons']:
            proof.update(e['id'] for e in events if e.get('kind') in ('pipe','valve_action','reservoir_level')
                         and node in (e['data'].get('from'),e['data'].get('to'),e['data'].get('node')))
        results.append((f'水量守恒:{node}',dict(
            discrepancy=info['discrepancy'],meters=info['meters'],frozen=info['frozen'],
            reasons=info['reasons'],edge_ids=sorted(e['edge_id'] for e in info['inflow']+info['outflow']),
            tolerance=reconciliation['tolerance']),sorted(proof)))
    save_decisions(db,subject,results)
    return results
