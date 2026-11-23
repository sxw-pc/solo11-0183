import json
import tempfile
import unittest
from pathlib import Path

from rangeledger import wildlife
from rangeledger.camera_trap import dedupe_same_labels, sort_frames, verify_frame
from rangeledger.store import connect, evidence, ingest, report, save_decisions
from rangeledger.wildlife import PhysicalConstraintError, ReidGraph, sightings

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'deer-census.json'
SUBJECT = 'deer-census-1'
INTERVAL = wildlife.INTERVAL_SURFACE
PRECISE = wildlife.PRECISE_SURFACE


def load_events():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))['events']


def frame_dicts(events):
    return [dict(id=e['id'], **e['data']) for e in events if e['kind'] == 'camera_frame']


def signature_event(sid, verdict=None, revokes=None, photo=None, day=0, time=500, reviewer='R1'):
    data = {'edge': 'census-edge-tail', 'reviewer': reviewer}
    if verdict:
        data['verdict'] = verdict
        data['photo'] = photo
        data['photo_day'] = day
    if revokes:
        data['revokes'] = revokes
    return {'id': sid, 'kind': 'review_signature', 'subject': SUBJECT,
            'event_time': time, 'received_at': time, 'data': data}


class DeerCensusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'census.db')
        self.events = load_events()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def run_pipeline(self, extra=()):
        with self.db:
            for event in list(self.events) + list(extra):
                ingest(self.db, event)
            records = evidence(self.db, SUBJECT)
            save_decisions(self.db, SUBJECT, wildlife.decide(records))
        return report(self.db, SUBJECT)

    def versions(self, surface):
        rows = self.db.execute(
            'SELECT version,result,evidence_ids FROM decision '
            'WHERE subject=? AND surface=? ORDER BY version',
            (SUBJECT, surface)).fetchall()
        return [(r['version'], json.loads(r['result']), json.loads(r['evidence_ids']))
                for r in rows]

    def test_four_local_detections_stay_detection_count(self):
        frames = frame_dicts(self.events)
        self.assertEqual(len(frames), 2)
        for frame in frames:
            self.assertTrue(verify_frame(frame))
        ordered = sort_frames(frames)
        self.assertEqual([f['camera'] for f in ordered], ['A', 'B'])
        self.assertEqual(sightings(frames), 4)          # 检测条数
        self.assertEqual(dedupe_same_labels(frames), 4)  # 常规补丁去重后仍是四条局部标签
        rep = self.run_pipeline()
        interval = rep[INTERVAL]['result']
        self.assertEqual(interval['raw_detections'], 4)
        self.assertEqual((interval['lower'], interval['upper']), (3, 4))
        self.assertNotEqual(interval['lower'], interval['raw_detections'])

    def test_confirmed_and_pending_edges_with_provenance(self):
        rep = self.run_pipeline()
        interval = rep[INTERVAL]['result']
        self.assertEqual(interval['components'], 3)
        self.assertEqual(len(interval['confirmed']), 1)
        self.assertEqual(interval['confirmed'][0]['pair'],
                         ['census-frame-a-0800#a1', 'census-frame-b-0804#b1'])
        self.assertEqual(interval['confirmed'][0]['basis'], '耳斑特征')
        self.assertEqual(len(interval['pending']), 1)
        self.assertEqual(interval['pending'][0]['pair'],
                         ['census-frame-a-0800#a2', 'census-frame-b-0804#b2'])
        self.assertEqual(interval['pending'][0]['edge'], 'census-edge-tail')
        precise = rep[PRECISE]['result']
        self.assertEqual(precise['status'], '待复核')
        self.assertEqual(precise['open'], ['census-edge-tail'])
        self.assertEqual(set(rep[INTERVAL]['evidence_ids']),
                         {'census-frame-a-0800', 'census-frame-b-0804',
                          'census-edge-earspot', 'census-edge-tail'})

    def test_supplementary_same_gives_exact_three_and_keeps_old_versions(self):
        self.run_pipeline()
        rep = self.run_pipeline([signature_event(
            'census-sig-1', verdict='same', photo='eartag-d1-0806', time=500)])
        precise = rep[PRECISE]['result']
        self.assertEqual(precise['status'], '已签名')
        self.assertEqual(precise['total'], 3)
        self.assertEqual(precise['resolutions'][0]['signature'], 'census-sig-1')
        self.assertEqual(precise['resolutions'][0]['verdict'], '同只')
        interval = rep[INTERVAL]['result']
        self.assertEqual((interval['lower'], interval['upper']), (3, 3))
        hist = self.versions(INTERVAL)
        self.assertEqual([v for v, _, _ in hist], [1, 2])
        self.assertEqual((hist[0][1]['lower'], hist[0][1]['upper']), (3, 4))  # 旧版保留
        phist = self.versions(PRECISE)
        self.assertEqual([v for v, _, _ in phist], [1, 2])
        self.assertEqual(phist[0][1]['status'], '待复核')
        self.assertIn('census-sig-1', phist[1][2])

    def test_supplementary_distinct_gives_exact_four(self):
        self.run_pipeline()
        rep = self.run_pipeline([signature_event(
            'census-sig-1', verdict='distinct', photo='eartag-d2-0807', time=500)])
        precise = rep[PRECISE]['result']
        self.assertEqual(precise['status'], '已签名')
        self.assertEqual(precise['total'], 4)
        self.assertEqual(precise['resolutions'][0]['verdict'], '异只')
        interval = rep[INTERVAL]['result']
        self.assertEqual((interval['lower'], interval['upper']), (4, 4))

    def test_revocation_recomputes_versions(self):
        sig1 = signature_event('census-sig-1', verdict='same',
                               photo='eartag-d1-0806', time=500)
        revoke = signature_event('census-revoke-1', revokes='census-sig-1',
                                 time=510, reviewer='R2')
        sig2 = signature_event('census-sig-2', verdict='distinct',
                               photo='eartag-d2-0807', time=520)
        self.run_pipeline()
        self.run_pipeline([sig1])
        rep = self.run_pipeline([sig1, revoke])
        self.assertEqual(rep[PRECISE]['result']['status'], '待复核')
        self.assertEqual((rep[INTERVAL]['result']['lower'],
                          rep[INTERVAL]['result']['upper']), (3, 4))
        rep = self.run_pipeline([sig1, revoke, sig2])
        self.assertEqual(rep[PRECISE]['result']['status'], '已签名')
        self.assertEqual(rep[PRECISE]['result']['total'], 4)
        hist = self.versions(INTERVAL)
        phist = self.versions(PRECISE)
        self.assertEqual([v for v, _, _ in hist], [1, 2, 3, 4])
        self.assertEqual([v for v, _, _ in phist], [1, 2, 3, 4])
        self.assertEqual(phist[1][1]['total'], 3)   # 被撤销的签名版次仍保留
        self.assertEqual(phist[2][1]['status'], '待复核')
        self.assertEqual(phist[3][1]['total'], 4)

    def test_physical_constraint_same_instant_nonoverlap(self):
        fa = {'id': 'fA', 'camera': 'A', 'captured_at': 480, 'photo': 'pA',
              'detections': [{'label': 'a1'}], 'overlaps': ['A']}
        fb = {'id': 'fB', 'camera': 'B', 'captured_at': 480, 'photo': 'pB',
              'detections': [{'label': 'b1'}], 'overlaps': ['B']}
        graph = ReidGraph()
        graph.add_frame(fa)
        graph.add_frame(fb)
        with self.assertRaises(PhysicalConstraintError):
            graph.add_edge('e-x', 'fA#a1', 'fB#b1', 'confirmed', '耳斑')
        with self.assertRaises(PhysicalConstraintError):
            graph.add_edge('e-y', 'fA#a1', 'fB#b1', 'pending', '尾纹')
        moved = ReidGraph()  # 鹿可在四分钟内穿行：不同时刻允许关联
        moved.add_frame(fa)
        moved.add_frame(dict(fb, captured_at=484))
        moved.add_edge('e-z', 'fA#a1', 'fB#b1', 'confirmed', '耳斑')
        self.assertEqual(len(moved.components()), 1)
        with self.assertRaises(PhysicalConstraintError):  # 同一帧内不能合并
            moved.add_edge('e-w', 'fA#a1', 'fA#a1', 'confirmed', '耳斑')

    def test_incremental_edge_updates_components_and_bounds(self):
        graph = ReidGraph()
        for frame in sort_frames(frame_dicts(self.events)):
            graph.add_frame(frame)
        self.assertEqual(len(graph.components()), 4)
        self.assertEqual(graph.bounds(), (4, 4))
        graph.add_edge('census-edge-earspot', 'census-frame-a-0800#a1',
                       'census-frame-b-0804#b1', 'confirmed', '耳斑特征')
        self.assertEqual(len(graph.components()), 3)   # 确定边增量合并分量
        self.assertEqual(graph.bounds(), (4, 4))
        graph.add_edge('census-edge-tail', 'census-frame-a-0800#a2',
                       'census-frame-b-0804#b2', 'pending', '尾纹相似')
        self.assertEqual(graph.bounds(), (3, 4))       # 待核边拉开区间
        graph.apply_resolution('census-edge-tail', 'same')
        self.assertEqual(graph.bounds(), (3, 3))       # 签名同只扣减基线
        self.assertEqual(len(graph.components()), 2)

    def test_supplementary_requires_same_day_photo(self):
        self.run_pipeline()
        before = report(self.db, SUBJECT)
        with self.assertRaises(ValueError):
            self.run_pipeline([signature_event(
                'census-sig-bad', verdict='same', photo='eartag-x', day=1, time=500)])
        self.assertEqual(before, report(self.db, SUBJECT))

    def test_frames_are_immutable_under_supplementary(self):
        self.run_pipeline()
        before = report(self.db, SUBJECT)
        tampered = dict(self.events[0])
        tampered['data'] = dict(self.events[0]['data'], photo='ph-FORGED')
        with self.assertRaises(ValueError):
            self.run_pipeline([tampered])  # 补证不能追写旧照片
        self.assertEqual(before, report(self.db, SUBJECT))

    def test_replay_idempotent(self):
        extra = [signature_event('census-sig-1', verdict='same',
                                 photo='eartag-d1-0806', time=500)]
        self.run_pipeline()
        self.run_pipeline(extra)
        first = report(self.db, SUBJECT)
        self.run_pipeline(extra)  # 重放：同样证据再跑一遍
        second = report(self.db, SUBJECT)
        self.assertEqual(first, second)
        self.assertEqual([v for v, _, _ in self.versions(INTERVAL)], [1, 2])
        self.assertEqual([v for v, _, _ in self.versions(PRECISE)], [1, 2])
        records = evidence(self.db, SUBJECT)  # 从原始证据重算与留存版次一致
        computed = {name: (result, sorted(set(ids)))
                    for name, result, ids in wildlife.decide(records)}
        self.assertEqual(set(computed), {INTERVAL, PRECISE})
        for name, (result, ids) in computed.items():
            self.assertEqual(second[name]['result'], result)
            self.assertEqual(second[name]['evidence_ids'], ids)


if __name__ == '__main__':
    unittest.main()
