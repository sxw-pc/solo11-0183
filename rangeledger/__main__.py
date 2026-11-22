import argparse, json, sys
from . import __file__ as module_file
from .store import connect, ingest, evidence, save_decisions, report
from . import MODULES

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
    return {s:report(db,s) for s in sorted(subjects)}

def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=['init','demo','report']+[m.COMMAND for m in MODULES])
    p.add_argument('--db',default='work.db')
    p.add_argument('--fixture')
    p.add_argument('--subject',default='case-1')
    args=p.parse_args(argv)
    db=connect(args.db)
    if args.command=='init': out={'db':args.db,'module_file':module_file}
    elif args.command=='report': out=report(db,args.subject)
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
