import tempfile, unittest
from pathlib import Path
import rangeledger
from rangeledger.store import connect, ingest, evidence, save_decisions, report
from rangeledger.__main__ import run_fixture

class BusinessDecisions(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=connect(Path(self.temp.name)/'case.db')
        for module in rangeledger.MODULES: run_fixture(self.db,module.NORMAL)
    def tearDown(self): self.db.close(); self.temp.cleanup()
    def add(self,kind,data,time=90):
        subject='case-1'
        with self.db:
            ingest(self.db,dict(id=f'extra-{kind}-{time}',kind=kind,subject=subject,event_time=time,received_at=time+100,data=data))
            records=evidence(self.db,subject)
            results=[]
            for module in rangeledger.MODULES: results.extend(module.decide(records))
            save_decisions(self.db,subject,results)
    def result(self,name): return report(self.db,'case-1')[name]['result']
    def test_capacity_reduction_changes_compensation(self):
        self.assertEqual(self.result('跨区占用分配'),4)
        self.assertEqual(self.result('超载补偿分摊'),0)
        self.add('vegetation_sample',{'grass':0.2},91)
        self.assertEqual(self.result('旱季草量折减'),2)
        self.assertEqual(self.result('超载补偿分摊'),20)
    def test_quarantine_blocks_movement(self):
        self.assertEqual(self.result('防疫隔离通行'),'允许')
        self.add('quarantine',{'isolated':True},92)
        self.assertEqual(self.result('防疫隔离通行'),'隔离')
        self.assertEqual(self.result('植被恢复阈值判定'),'休牧')
    def test_breach_adds_responsibility(self):
        self.assertEqual(self.result('夜间越界巡护'),0)
        self.add('movement',{'start':94,'end':95,'count':1,'outside':True},93)
        self.assertEqual(self.result('夜间越界巡护'),1)
        self.assertGreater(self.result('超载补偿分摊'),0)
