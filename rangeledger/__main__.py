import argparse, json, sys
from . import __file__ as module_file
from .store import connect, ingest, evidence, save_decisions, report
from . import MODULES
from . import schedule

def _next_alloc_version(db, subject):
    found = db.execute(
        'SELECT m.payload FROM movement m JOIN evidence e ON e.id=m.id WHERE e.subject=?',
        (subject,)).fetchall()
    versions = [json.loads(r['payload']).get('alloc_version', 0) for r in found]
    return max(versions, default=0) + 1

def run_fixture(db, path, allowed=None):
    events=json.loads(__import__('pathlib').Path(path).read_text(encoding='utf-8'))['events']
    if allowed and any(e['kind'] not in allowed for e in events): raise ValueError('fixture contains another subsystem')
    subjects=set()
    with db:
        for event in events:
            ingest(db,event); subjects.add(event['subject'])
        for subject in subjects:
            records=evidence(db,subject)
            results=[]
            for module in MODULES: results.extend(module.decide(records))
            save_decisions(db,subject,results)
            # 含预约的案例：把当班分配表投影成 movement，再经三入口复核落库。
            if any(e['kind']=='booking' for e in records):
                version=_next_alloc_version(db,subject)
                for projected in schedule.projected_movements(records,version):
                    ingest(db,projected)
                records=evidence(db,subject)
                _,triples=schedule.adjudicate(records,version)
                save_decisions(db,subject,triples)
    return {s:report(db,s) for s in sorted(subjects)}

def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=['init','demo','report','replay']+[m.COMMAND for m in MODULES])
    p.add_argument('--db',default='work.db')
    p.add_argument('--fixture')
    p.add_argument('--subject',default='case-1')
    args=p.parse_args(argv)
    db=connect(args.db)
    if args.command=='init': out={'db':args.db,'module_file':module_file}
    elif args.command=='report': out=report(db,args.subject)
    elif args.command=='replay':
        records=evidence(db,args.subject)
        computed={}
        for module in MODULES:
            for name,value,ids in module.decide(records): computed[name]={'result':value,'evidence_ids':sorted(set(ids))}
        stored=report(db,args.subject)
        mismatches={name:{'stored':stored.get(name),'recomputed':item} for name,item in computed.items() if not stored.get(name) or stored[name]['result']!=item['result'] or stored[name]['evidence_ids']!=item['evidence_ids']}
        out={'match':bool(records) and not mismatches,'mismatches':mismatches,'evidence_count':len(records)}
    elif args.command=='demo':
        out={}
        for module in MODULES:
            out.update(run_fixture(db,module.NORMAL))
    else:
        module=next(m for m in MODULES if m.COMMAND==args.command)
        out=run_fixture(db,args.fixture or module.NORMAL,set(module.KINDS))
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0

if __name__=='__main__': raise SystemExit(main())
