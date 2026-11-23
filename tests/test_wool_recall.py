import json, tempfile, unittest
from pathlib import Path
from rangeledger import store, product_sale
from rangeledger.medicine import withdrawal_end
from rangeledger.product_sale import may_ship
from rangeledger.store import connect, ingest, evidence, save_decisions, report

ROOT=Path(__file__).resolve().parents[1]
FIXTURE=ROOT/'fixtures'/'wool-sale-recall.json'
SUBJECT='case-wool-1'
ELIG='休药期销售资格'
RECALL='已交割追溯撤销单'
LEDGER='实物处置账'

def load():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))

class WoolRecallTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=connect(Path(self.tmp.name)/'case.db')
    def tearDown(self):
        self.db.close(); self.tmp.cleanup()
    def stage(self, events):
        results=[]
        with self.db:
            for e in events:
                results.append(ingest(self.db,e))
            records=evidence(self.db,SUBJECT)
            save_decisions(self.db,SUBJECT,product_sale.decide(records))
        return results
    def surface(self, name):
        return report(self.db,SUBJECT)[name]
    def versions(self, name):
        rows=self.db.execute('SELECT version,result FROM decision WHERE subject=? AND surface=? ORDER BY version',(SUBJECT,name)).fetchall()
        return [(r['version'],json.loads(r['result'])) for r in rows]

    def test_new_evidence_kinds_created(self):
        for kind in ('drug_treatment','wool_batch','sale_receipt','correction_link'):
            self.assertIn(kind,store.ENTITIES)

    def test_pure_function_baseline(self):
        t1={'day':3,'withdraw_days':7}
        t2={'day':8,'withdraw_days':7}
        self.assertEqual(withdrawal_end([t1]),10)
        self.assertEqual(withdrawal_end([t1,t2]),15)
        self.assertTrue(may_ship([t1],11))
        self.assertFalse(may_ship([t1,t2],11))

    def test_day11_true_then_day12_retrospective_false(self):
        data=load()
        first=[e for e in data['events'] if e['received_at']<=11]
        late=[e for e in data['events'] if e['received_at']>11]
        self.stage(first)
        self.assertEqual(self.surface(ELIG)['result'],{'W40':True})
        self.assertEqual(self.surface(ELIG)['version'],1)
        self.assertEqual(self.surface(RECALL)['result'],{})
        self.assertEqual(self.surface(LEDGER)['result'],{})
        self.stage(late)
        self.assertEqual(self.surface(ELIG)['result'],{'W40':False})
        self.assertEqual(self.surface(ELIG)['version'],2)
        recall=self.surface(RECALL)['result']['W40']
        self.assertEqual(recall['receipt'],'R1')
        self.assertEqual(recall['buyer'],'B')
        self.assertEqual(recall['qty_kg'],40)
        self.assertEqual(recall['amount'],4000)
        self.assertEqual(recall['status'],'追溯撤销')
        ledger=self.surface(LEDGER)['result']['W40']
        self.assertEqual(ledger['route'],'pending')
        self.assertEqual(ledger['receivable_balance'],4000)
        versions=self.versions(ELIG)
        self.assertEqual([v for v,_ in versions],[1,2])
        self.assertEqual(versions[0][1],{'W40':True})
        self.assertEqual(versions[1][1],{'W40':False})
        ids=set(self.surface(RECALL)['evidence_ids'])
        self.assertTrue({'wool-r1','wool-t2','wool-c1'}<=ids)
        self.assertIn('wool-r1',{e['id'] for e in evidence(self.db,SUBJECT)})

    def test_route_buyer_return(self):
        data=load()
        self.stage(data['events'])
        self.stage(data['routes']['buyer_return'])
        ledger=self.surface(LEDGER)['result']['W40']
        self.assertEqual(ledger['route'],'buyer_return')
        self.assertEqual(ledger['buyer_return_kg'],40)
        self.assertEqual(ledger['ranch_quarantine_kg'],40)
        self.assertEqual(ledger['disposal_transfer_kg'],0)
        self.assertEqual(ledger['reversed_amount'],4000)
        self.assertEqual(ledger['receivable_balance'],0)
        self.assertEqual(self.surface(RECALL)['result']['W40']['amount'],4000)

    def test_route_disposal_transfer(self):
        data=load()
        self.stage(data['events'])
        self.stage(data['routes']['disposal'])
        ledger=self.surface(LEDGER)['result']['W40']
        self.assertEqual(ledger['route'],'disposal_transfer')
        self.assertEqual(ledger['disposal_transfer_kg'],40)
        self.assertEqual(ledger['ranch_quarantine_kg'],0)
        self.assertEqual(ledger['buyer_return_kg'],0)
        self.assertEqual(ledger['reversed_amount'],4000)
        self.assertEqual(ledger['receivable_balance'],0)
        self.assertEqual(ledger['to'],'disposal_vendor')

    def test_same_recall_order_in_both_routes(self):
        data=load()
        recalls=[]
        for route in ('buyer_return','disposal'):
            with tempfile.TemporaryDirectory() as d:
                db=connect(Path(d)/'case.db')
                with db:
                    for e in data['events']+data['routes'][route]:
                        ingest(db,e)
                    save_decisions(db,SUBJECT,product_sale.decide(evidence(db,SUBJECT)))
                recalls.append(report(db,SUBJECT)[RECALL]['result'])
                db.close()
        self.assertEqual(recalls[0],recalls[1])
        self.assertEqual(recalls[0]['W40']['qty_kg'],40)
        self.assertEqual(recalls[0]['W40']['amount'],4000)

    def test_disposal_needs_test_and_permit(self):
        data=load()
        by_doc={e['data']['doc']:e for e in data['routes']['disposal']}
        self.stage(data['events'])
        self.stage([by_doc['disposal_transfer']])
        ledger=self.surface(LEDGER)['result']['W40']
        self.assertEqual(ledger['route'],'pending')
        self.assertEqual(ledger['reversed_amount'],0)
        self.assertEqual(ledger['receivable_balance'],4000)
        self.stage([by_doc['independent_test']])
        ledger=self.surface(LEDGER)['result']['W40']
        self.assertEqual(ledger['route'],'pending')
        self.assertEqual(ledger['receivable_balance'],4000)

    def test_reimport_same_originals_is_idempotent(self):
        data=load()
        first=self.stage(data['events'])
        self.assertTrue(all(first))
        before=report(self.db,SUBJECT)
        count=self.db.execute('SELECT COUNT(*) c FROM decision WHERE subject=?',(SUBJECT,)).fetchone()['c']
        again=self.stage(data['events'])
        self.assertTrue(all(not x for x in again))
        self.assertEqual(before,report(self.db,SUBJECT))
        count2=self.db.execute('SELECT COUNT(*) c FROM decision WHERE subject=?',(SUBJECT,)).fetchone()['c']
        self.assertEqual(count,count2)

    def test_conflicting_reimport_rejected(self):
        data=load()
        self.stage(data['events'])
        tampered=dict(data['events'][0])
        tampered['data']=dict(data['events'][0]['data'],withdraw_days=30)
        with self.assertRaises(ValueError):
            ingest(self.db,tampered)
        self.assertEqual(self.surface(ELIG)['result'],{'W40':False})

if __name__=='__main__':
    unittest.main()
