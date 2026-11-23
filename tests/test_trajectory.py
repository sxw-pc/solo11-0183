import json, tempfile, unittest
from pathlib import Path
import rangeledger
from rangeledger.track import sign_payload
from rangeledger.store import connect, ingest, evidence, save_decisions, report
from rangeledger.__main__ import run_fixture

ROOT=Path(__file__).resolve().parents[1]
NIGHT=ROOT/'fixtures/track_night.json'
FENCE={"zone":"Z1","eps":0.5,"polygon":[[0,0],[10,0],[10,10],[0,10]],"gates":[{"name":"E","point":[10,5]},{"name":"S","point":[5,0]}]}

def gps(holder,movement,fixes,device="collar-7"):
    data={"holder":holder,"movement":movement,"device":device,"fixes":fixes}
    data["signature"]=sign_payload(data)
    return data

def event(eid,kind,subject,t,data,r=None):
    return dict(id=eid,kind=kind,subject=subject,event_time=t,received_at=r if r is not None else t,data=data)

class TrajectoryCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=connect(Path(self.tmp.name)/'case.db')
    def tearDown(self): self.db.close(); self.tmp.cleanup()
    def load(self,subject,events):
        with self.db:
            for e in events: ingest(self.db,e)
            records=evidence(self.db,subject)
            results=[]
            for module in rangeledger.MODULES: results.extend(module.decide(records))
            save_decisions(self.db,subject,results)
    def add(self,subject,kind,eid,data,t,r=None):
        self.load(subject,[event(eid,kind,subject,t,data,r)])
    def result(self,name,subject='case-night'): return report(self.db,subject)[name]['result']
    def version(self,name,subject='case-night'): return report(self.db,subject)[name]['version']
    def synthetic(self,subject,mid,start,end,fixes,move_data=None):
        move=dict(holder="H1",herd="herd-1",zone="Z1",start=start,end=end,count=1)
        if move_data: move.update(move_data)
        events=[event('permit-'+subject,'permit',subject,20,{"holder":"H1","quota":10,"start":20,"end":40}),
                event('fence-'+subject,'fence',subject,18,FENCE),
                event(mid,'movement',subject,start,move),
                event('gps-'+subject,'gps_package',subject,int(start+1),gps("H1",mid,fixes))]
        self.load(subject,events)

class TestBooleanComparison(TrajectoryCase):
    def boolean_events(self,outside):
        move={"holder":"H1","herd":"herd-1","zone":"Z1","start":21,"end":23,"count":1}
        if outside is not None: move['outside']=outside
        return [event('permit-b','permit','case-b',20,{"holder":"H1","quota":10,"start":20,"end":24}),
                event('move-b','movement','case-b',21,move)]
    def decide(self,events):
        movement=next(m for m in rangeledger.MODULES if m.COMMAND=='movement')
        recovery=next(m for m in rangeledger.MODULES if m.COMMAND=='recovery')
        m={n:v for n,v,_ in movement.decide(events)}
        r={n:v for n,v,_ in recovery.decide(events)}
        return m,r
    def test_endpoints_inside_boolean_gives_zero(self):
        # 只用“起终点均在内”写 outside=false：两个既有 decide 都给 0
        m,r=self.decide(self.boolean_events(False))
        self.assertEqual(m['夜间越界巡护'],0)
        self.assertEqual(r['超载补偿分摊'],{'H1':0.0})
    def test_whole_movement_outside_gives_two_hours_ten(self):
        # 发现中途出界就把整条写 outside=true：各计 2 小时，H1 补偿加 10
        m,r=self.decide(self.boolean_events(True))
        self.assertEqual(m['夜间越界巡护'],2.0)
        self.assertEqual(r['超载补偿分摊'],{'H1':10.0})
    def test_normal_fixtures_keep_zero_night_breach(self):
        for module in rangeledger.MODULES: run_fixture(self.db,module.NORMAL)
        self.assertEqual(report(self.db,'case-1')['夜间越界巡护']['result'],0)
        self.assertEqual(report(self.db,'case-1')['超载补偿分摊']['result'],{'H1':0.0})

class TestNightCrossing(TrajectoryCase):
    def setUp(self):
        super().setUp()
        run_fixture(self.db,NIGHT)
    def test_segmentwise_one_hour_five_compensation(self):
        self.assertEqual(self.result('夜间越界巡护'),1.0)
        self.assertEqual(self.result('超载补偿分摊'),{'H1':5.0})
    def test_violation_table_has_boundary_times_and_sources(self):
        table=self.result('越界片段明细')
        self.assertEqual(len(table),1)
        row=table[0]
        self.assertEqual((row['state'],row['start'],row['end'],row['night']),('越界',21.5,22.5,1.0))
        self.assertEqual((row['exit']['time'],row['exit']['gate']),(21.5,'E'))
        self.assertEqual((row['entry']['time'],row['entry']['gate']),(22.5,'S'))
        self.assertGreater(row['exit']['error'],0)
        self.assertEqual(row['evidence'],['fence-z1','gps-h1-night'])
        self.assertEqual(self.result('轨迹采录核验')['accepted'],['gps-h1-night'])
    def test_full_transit_permit_clears_violation_but_keeps_path(self):
        self.add('case-night','transit_permit','tp-1',{"permit_id":"tp-1","holder":"H1","corridor":["E","S"],"valid_from":21,"valid_to":23,"signed_at":20},20)
        self.assertEqual(self.result('夜间越界巡护'),0.0)
        self.assertEqual(self.result('超载补偿分摊'),{'H1':0.0})
        table=self.result('越界片段明细')
        self.assertEqual(len(table),1)
        self.assertEqual((table[0]['state'],table[0]['permit']),('许可覆盖','tp-1'))
        self.assertEqual(self.result('许可叠置覆盖')['tp-1']['covered'],[[21.5,22.5]])
    def test_permit_signed_at_22_covers_only_latter_half(self):
        self.add('case-night','transit_permit','tp-2',{"permit_id":"tp-2","holder":"H1","corridor":["E","S"],"valid_from":21,"valid_to":23,"signed_at":22},22)
        self.assertEqual(self.result('夜间越界巡护'),0.5)
        self.assertEqual(self.result('超载补偿分摊'),{'H1':2.5})
        states={(r['start'],r['end']):r['state'] for r in self.result('越界片段明细')}
        self.assertEqual(states,{(21.5,22.0):'越界',(22.0,22.5):'许可覆盖'})
    def test_late_upload_cannot_backdate_approval(self):
        self.add('case-night','transit_permit','tp-3',{"permit_id":"tp-3","holder":"H1","corridor":["E","S"],"valid_from":21,"valid_to":23,"signed_at":20},20,r=24)
        self.assertEqual(self.result('夜间越界巡护'),1.0)
        self.assertEqual(self.result('超载补偿分摊'),{'H1':5.0})
        self.assertEqual(self.result('许可叠置覆盖')['tp-3']['effective_from'],24.0)
        self.assertEqual(self.result('许可叠置覆盖')['tp-3']['covered'],[])
    def test_permit_revocation_restores_latter_half(self):
        self.add('case-night','transit_permit','tp-1',{"permit_id":"tp-1","holder":"H1","corridor":["E","S"],"valid_from":21,"valid_to":23,"signed_at":20},20)
        self.assertEqual(self.result('夜间越界巡护'),0.0)
        v_before=self.version('越界片段明细')
        self.add('case-night','transit_permit','tp-1-rev',{"permit_id":"tp-1","holder":"H1","corridor":["E","S"],"valid_from":21,"valid_to":23,"signed_at":20,"revoked":True,"revoked_at":22},22)
        self.assertEqual(self.result('夜间越界巡护'),0.5)
        self.assertEqual(self.result('超载补偿分摊'),{'H1':2.5})
        permit=self.result('许可叠置覆盖')['tp-1']
        self.assertTrue(permit['revoked'])
        self.assertEqual(permit['versions'],2)
        self.assertGreater(self.version('越界片段明细'),v_before)
    def test_duplicate_package_counted_once(self):
        dup=json.loads(json.dumps([e for e in json.loads(NIGHT.read_text(encoding='utf-8'))['events'] if e['id']=='gps-h1-night'][0]['data']))
        self.add('case-night','gps_package','gps-h1-night-copy',dup,24)
        self.assertEqual(self.result('轨迹采录核验')['duplicate'],['gps-h1-night-copy'])
        self.assertEqual(self.result('夜间越界巡护'),1.0)
        self.assertEqual(self.result('超载补偿分摊'),{'H1':5.0})
    def test_tampered_signature_rejected(self):
        bad=gps("H1","move-h1-night",[{"t":21,"x":5,"y":5},{"t":22,"x":50,"y":50},{"t":23,"x":5,"y":5}])
        bad['signature']='0'*64
        self.add('case-night','gps_package','gps-h1-hacked',bad,24)
        self.assertEqual(self.result('轨迹采录核验')['rejected'],[{'id':'gps-h1-hacked','reason':'签名不符'}])
        self.assertEqual(self.result('夜间越界巡护'),1.0)
    def test_overlapping_tracks_mark_conflict_unverified(self):
        self.add('case-night','gps_package','gps-h1-b',gps("H1","move-h1-night",[{"t":22,"x":5,"y":5},{"t":23,"x":6,"y":5},{"t":24,"x":5,"y":5}]),24)
        self.assertEqual(self.result('夜间越界巡护'),0.5)
        self.assertEqual(self.result('超载补偿分摊'),{'H1':2.5})
        table={ (r['start'],r['end']):r for r in self.result('越界片段明细')}
        self.assertEqual(table[(21.5,22.0)]['state'],'越界')
        self.assertEqual(table[(22.0,22.5)]['state'],'待核')
        self.assertEqual(table[(22.0,22.5)]['reason'],'包间冲突')
        self.assertEqual(table[(22.0,22.5)]['evidence'],['fence-z1','gps-h1-b','gps-h1-night'])
    def test_fence_revision_recomputes_and_versions(self):
        v_before=self.version('夜间越界巡护')
        self.add('case-night','fence','fence-z1-v2',{"zone":"Z1","eps":0.5,"polygon":[[0,0],[8,0],[8,10],[0,10]],"gates":[{"name":"E","point":[8,5]},{"name":"S","point":[5,0]}]},19)
        self.assertAlmostEqual(self.result('夜间越界巡护'),1.2,places=6)
        self.assertEqual(self.result('越界片段明细')[0]['evidence'],['fence-z1-v2','gps-h1-night'])
        self.assertGreater(self.version('夜间越界巡护'),v_before)
    def test_persistence_replay_and_idempotence(self):
        first=report(self.db,'case-night')
        self.assertEqual(first['越界片段明细']['evidence_ids'],['fence-z1','gps-h1-night'])
        self.assertEqual(first['夜间越界巡护']['evidence_ids'],['fence-z1','gps-h1-night'])
        with self.db:
            records=evidence(self.db,'case-night')
            results=[]
            for module in rangeledger.MODULES: results.extend(module.decide(records))
            save_decisions(self.db,'case-night',results)
        second=report(self.db,'case-night')
        self.assertEqual(first,second)

class TestTrajectoryEdgeCases(TrajectoryCase):
    def test_gap_not_filled_with_last_coordinate(self):
        self.synthetic('case-gap','move-gap',21,25,[{"t":21,"x":5,"y":5},{"t":25,"x":5,"y":5}])
        table=self.result('越界片段明细','case-gap')
        self.assertEqual(len(table),1)
        self.assertEqual((table[0]['state'],table[0]['reason']),('待核','断档'))
        self.assertEqual((table[0]['start'],table[0]['end']),(21.0,25.0))
        self.assertEqual(self.result('夜间越界巡护','case-gap'),0.0)
    def test_gps_jump_marked_unverified(self):
        self.synthetic('case-jump','move-jump',21,23,[{"t":21,"x":5,"y":5},{"t":21.1,"x":200,"y":200},{"t":21.2,"x":5,"y":5},{"t":22,"x":6,"y":5},{"t":23,"x":5,"y":5}])
        table=self.result('越界片段明细','case-jump')
        self.assertEqual(len(table),1)
        self.assertEqual((table[0]['state'],table[0]['reason']),('待核','跳点'))
        self.assertEqual(self.result('夜间越界巡护','case-jump'),0.0)
    def test_crossing_midnight_counts_both_night_windows(self):
        self.synthetic('case-mid','move-mid',23,25,[{"t":23,"x":5,"y":5},{"t":23.5,"x":10,"y":5},{"t":24,"x":14,"y":-4},{"t":24.5,"x":5,"y":0},{"t":25,"x":5,"y":4}])
        self.assertEqual(self.result('夜间越界巡护','case-mid'),1.0)
        self.assertEqual(self.result('超载补偿分摊','case-mid'),{'H1':5.0})
        row=self.result('越界片段明细','case-mid')[0]
        self.assertEqual((row['start'],row['end']),(23.5,24.5))
    def test_concave_notch_is_outside(self):
        notch=dict(FENCE,zone='Z2',polygon=[[0,0],[10,0],[10,3],[7,5],[10,7],[10,10],[0,10]],gates=[])
        events=[event('permit-notch','permit','case-notch',20,{"holder":"H1","quota":10,"start":20,"end":40}),
                event('fence-notch','fence','case-notch',18,notch),
                event('move-notch','movement','case-notch',21,{"holder":"H1","herd":"herd-1","zone":"Z2","start":21,"end":22,"count":1}),
                event('gps-notch','gps_package','case-notch',22,gps("H1","move-notch",[{"t":21,"x":8,"y":2},{"t":22,"x":8,"y":8}]))]
        self.load('case-notch',events)
        self.assertAlmostEqual(self.result('夜间越界巡护','case-notch'),2/9,places=6)
        row=self.result('越界片段明细','case-notch')[0]
        self.assertEqual(row['state'],'越界')
        self.assertIsNone(row['exit']['gate'])
        self.assertEqual((row['exit']['edge'],row['entry']['edge']),(2,3))

class TestTrajectoryModuleFixtures(TrajectoryCase):
    def test_clean_track_fixture_zero(self):
        run_fixture(self.db,ROOT/'fixtures/track_ok.json')
        self.assertEqual(self.result('夜间越界巡护','case-track'),0.0)
        self.assertEqual(self.result('越界片段明细','case-track'),[])
        self.assertEqual(self.result('轨迹采录核验','case-track')['accepted'],['gps-track-ok'])
    def test_tampered_failure_fixture_marks_duplicate_and_rejected(self):
        run_fixture(self.db,ROOT/'fixtures/track_ok.json')
        before=report(self.db,'case-track')['轨迹采录核验']['version']
        run_fixture(self.db,ROOT/'fixtures/track_tampered.json')
        check=self.result('轨迹采录核验','case-track')
        self.assertEqual(check['duplicate'],['gps-track-dup'])
        self.assertEqual(check['rejected'],[{'id':'gps-track-bad','reason':'签名不符'}])
        self.assertEqual(self.result('夜间越界巡护','case-track'),0.0)
        self.assertGreater(self.version('轨迹采录核验','case-track'),before)

if __name__=='__main__': unittest.main()
