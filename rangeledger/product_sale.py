from .medicine import withdrawal_end
from .common import rows

KINDS=['drug_treatment','wool_batch','sale_receipt','correction_link']

def may_ship(treatments, sale_day):
    return sale_day >= withdrawal_end(treatments)

def _course(treatments, flock):
    signed=[t for t in treatments if t['data'].get('flock')==flock and t['data'].get('signed')]
    signed.sort(key=lambda t:(t['data'].get('day',0), t['event_time'], t['id']))
    return signed

def _args(course):
    return [{'day':t['data'].get('day',0),'withdraw_days':t['data'].get('withdraw_days',0)} for t in course]

def _sales(ev):
    return [e for e in rows(ev,'sale_receipt') if e['data'].get('doc')=='sale']

def _docs(ev, doc, batch):
    return [e for e in rows(ev,'sale_receipt') if e['data'].get('doc')==doc and e['data'].get('batch')==batch]

def _latest(events):
    return max(events,key=lambda e:(e['event_time'],e['received_at'],e['id'])) if events else None

def _flock_of(sale, batches):
    batch=batches.get(sale['data'].get('batch'))
    return batch['data'].get('flock') if batch else sale['data'].get('flock')

def decide(ev):
    batches={e['data'].get('batch'):e for e in rows(ev,'wool_batch')}
    sales=_sales(ev)
    if not batches and not sales:
        return []
    treatments=rows(ev,'drug_treatment')
    links=rows(ev,'correction_link')
    courses={}
    def course_for(flock):
        if flock not in courses: courses[flock]=_course(treatments,flock)
        return courses[flock]

    eligibility={}
    elig_ids=[]
    for sale in sales:
        d=sale['data']; name=d.get('batch')
        course=course_for(_flock_of(sale,batches))
        eligibility[name]=may_ship(_args(course), d.get('ship_day',0))
        ids=[sale['id']]+[t['id'] for t in course]
        if name in batches: ids.append(batches[name]['id'])
        elig_ids+=ids

    recall={}
    recall_ids=[]
    for sale in sales:
        d=sale['data']; name=d.get('batch')
        course=course_for(_flock_of(sale,batches))
        recall_ids+=[sale['id']]+[t['id'] for t in course]
        if eligibility.get(name):
            continue
        linked=[l for l in links if l['data'].get('batch')==name or l['data'].get('receipt')==d.get('receipt')]
        recall[name]={'receipt':d.get('receipt'),'buyer':d.get('buyer'),'qty_kg':d.get('qty_kg'),
                      'amount':d.get('amount'),'ship_day':d.get('ship_day'),'status':'追溯撤销','stop_use':True}
        recall_ids+=[l['id'] for l in linked]

    ledger={}
    ledger_ids=[]
    for sale in sales:
        d=sale['data']; name=d.get('batch')
        ledger_ids.append(sale['id'])
        if name not in recall:
            continue
        amount=d.get('amount',0)
        ret=_latest(_docs(ev,'buyer_return',name))
        tr=_latest(_docs(ev,'disposal_transfer',name))
        tests={e['data'].get('receipt'):e for e in _docs(ev,'independent_test',name)}
        permits={e['data'].get('receipt'):e for e in _docs(ev,'disposal_permit',name)
                 if e['data'].get('signed') and e['data'].get('non_food')}
        if ret:
            qty=ret['data'].get('qty_kg',0)
            ledger[name]={'route':'buyer_return','status':'退货隔离','to':ret['data'].get('to'),
                          'buyer_return_kg':qty,'ranch_quarantine_kg':qty,'disposal_transfer_kg':0,
                          'reversed_amount':amount,'receivable_balance':0}
            ledger_ids.append(ret['id'])
        elif tr and tr['data'].get('test') in tests and tr['data'].get('permit') in permits:
            qty=tr['data'].get('qty_kg',0)
            ledger[name]={'route':'disposal_transfer','status':'非食用处置','to':tr['data'].get('to'),
                          'buyer_return_kg':0,'ranch_quarantine_kg':0,'disposal_transfer_kg':qty,
                          'reversed_amount':amount,'receivable_balance':0}
            ledger_ids+=[tr['id'],tests[tr['data'].get('test')]['id'],permits[tr['data'].get('permit')]['id']]
        else:
            ledger[name]={'route':'pending','status':'待处置','buyer_return_kg':0,'ranch_quarantine_kg':0,
                          'disposal_transfer_kg':0,'reversed_amount':0,'receivable_balance':amount}

    return [('休药期销售资格',eligibility,elig_ids),
            ('已交割追溯撤销单',recall,recall_ids),
            ('实物处置账',ledger,ledger_ids)]
