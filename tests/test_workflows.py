import json, tempfile, unittest
from pathlib import Path
import rangeledger
from rangeledger.store import connect, report, evidence
from rangeledger.__main__ import run_fixture, main

ROOT=Path(__file__).resolve().parents[1]
class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=connect(Path(self.tmp.name)/"case.db")
    def tearDown(self): self.db.close(); self.tmp.cleanup()
    def test_local_module(self):
        print(rangeledger.__file__)
        self.assertTrue(Path(rangeledger.__file__).resolve().is_relative_to(ROOT))
    def test_three_subsystems_and_ten_decisions(self):
        for module in rangeledger.MODULES: run_fixture(self.db,module.NORMAL)
        report_now=report(self.db,"case-1")
        self.assertEqual(len(report_now),10)
        self.assertTrue(all(v["evidence_ids"] for v in report_now.values()))
    def test_idempotence_and_conflicting_identity(self):
        mod=rangeledger.MODULES[0]; run_fixture(self.db,mod.NORMAL)
        before=report(self.db,"case-1")
        run_fixture(self.db,mod.NORMAL)
        self.assertEqual(before,report(self.db,"case-1"))
        rows=json.loads(Path(mod.NORMAL).read_text(encoding="utf8"))
        rows["events"][0]["data"]["different"]=True
        p=Path(self.tmp.name)/"conflict.json";p.write_text(json.dumps(rows),encoding="utf8")
        with self.assertRaises(ValueError): run_fixture(self.db,p)
        self.assertEqual(before,report(self.db,"case-1"))
    def test_replay_detects_tampered_decision(self):
        for module in rangeledger.MODULES: run_fixture(self.db,module.NORMAL)
        records=evidence(self.db,"case-1")
        computed={}
        for module in rangeledger.MODULES:
            for name,value,ids in module.decide(records): computed[name]=(value,sorted(set(ids)))
        stored=report(self.db,"case-1")
        self.assertTrue(all(stored[name]["result"]==value and stored[name]["evidence_ids"]==ids for name,(value,ids) in computed.items()))
        with self.db:
            self.db.execute("UPDATE decision SET result='999' WHERE surface=?",(next(iter(computed)),))
        altered=report(self.db,"case-1")
        self.assertNotEqual(altered[next(iter(computed))]["result"],computed[next(iter(computed))][0])
    def test_late_boundary_replays_decisions(self):
        for mod in rangeledger.MODULES: run_fixture(self.db,mod.NORMAL)
        before=report(self.db,"case-1")
        for mod in rangeledger.MODULES: run_fixture(self.db,ROOT/mod.FAILURE)
        after=report(self.db,"case-1")
        self.assertEqual(len(after),10)
        self.assertTrue(any(after[k]["version"]>before[k]["version"] for k in before))
        self.assertEqual(len(evidence(self.db,"case-1")),18)

