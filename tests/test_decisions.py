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
        self.assertEqual(self.result('跨区占用分配'),{'P1':4.0})
        self.assertEqual(self.result('超载补偿分摊'),{'H1':0.0})
        self.add('vegetation_sample',{'grass':0.2},91)
        self.assertEqual(self.result('旱季草量折减'),2)
        self.assertEqual(self.result('超载补偿分摊'),{'H1':20.0})
    def test_quarantine_blocks_movement(self):
        self.assertEqual(self.result('防疫隔离通行'),'允许')
        self.add('quarantine',{'isolated':True},92)
        self.assertEqual(self.result('防疫隔离通行'),'隔离')
        self.assertEqual(self.result('植被恢复阈值判定'),'休牧')
    def test_breach_adds_responsibility(self):
        self.assertEqual(self.result('夜间越界巡护'),0)
        self.add('movement',{'start':94,'end':95,'count':1,'outside':True},93)
        self.assertEqual(self.result('夜间越界巡护'),1.0)
        self.assertGreater(sum(self.result('超载补偿分摊').values()),0)
    def test_transfer_balances_and_concurrent_oversubscription(self):
        self.add('allocation',{'from':'H1','to':'H2','amount':6},94)
        self.assertEqual(self.result('份额转让守恒'),{'H1':4.0,'H2':6.0})
        self.add('allocation',{'from':'H1','to':'H3','amount':5},95)
        self.assertEqual(self.result('份额转让守恒'),'超配')
    def test_overlapping_same_herd_is_one_occupation(self):
        self.add('movement',{'herd':'herd-1','zone':'P1','holder':'H1','start':2,'end':4,'count':2},96)
        self.assertEqual(self.result('跨区占用分配'),{'P1':6.0})
        self.assertEqual(self.result('许可期限放行'),'禁入')
    def test_night_breach_uses_actual_duration(self):
        self.add('movement',{'herd':'herd-2','zone':'P1','holder':'H2','start':21,'end':23,'count':1,'outside':True},97)
        self.assertEqual(self.result('夜间越界巡护'),2.0)
        self.assertGreater(self.result('超载补偿分摊')['H2'],0)
    def test_rest_release_requires_new_vegetation_evidence(self):
        self.add('rest_order',{'active':True,'deadline':3},98)
        self.assertEqual(self.result('休牧令执行与重开'),'清场中')
        self.add('rest_order',{'active':False},99)
        self.assertEqual(self.result('休牧令执行与重开'),'待复核')
        self.add('vegetation_sample',{'grass':0.8},100)
        self.assertEqual(self.result('休牧令执行与重开'),'重开')
