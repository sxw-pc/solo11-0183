"""鹿只普查：个体重识别图、可辨识总数区间与调查结论版次。

规程约定（两条获授权调查规程）：
- 相机检测条数只是基线，不是鹿只总数；sightings 保留此义。
- 审慎区间规程只合并确定边，区间跨未决待核边：
  [基线 - 待核边数 - 已签同只数, 基线 - 已签同只数]。
- 现场补证规程以同日耳标照片和人工复核签名裁决待核边，
  每确认一对同只扣减基线一条，给出具签名的精确普查；
  补证不追写旧照片，撤销签名时重算相应版次。
- 确定边进入关联图与物理约束校验：一只鹿不能在同一时刻
  被两台视野互不重叠的相机占用。
"""
from .camera_trap import visible_count, verify_frame, sort_frames
from .common import rows

INTERVAL_SURFACE = '廊道鹿只区间普查'
PRECISE_SURFACE = '廊道鹿只精确普查'

VERDICT_TEXT = {'same': '同只', 'distinct': '异只'}


def sightings(frames):
    return sum(visible_count(frame) for frame in frames)


class PhysicalConstraintError(ValueError):
    """同一时刻被两台互不重叠的相机占用等同一只鹿的物理矛盾。"""


class ReidGraph:
    """个体重识别图：节点为检测，边带来源（确定/待核），增量维护连通分量与上下界。"""

    def __init__(self):
        self.parent = {}
        self.nodes = {}       # ref -> {'frame','camera','captured_at','label','overlaps'}
        self.confirmed = []   # 确定边（含来源）
        self.pending = []     # 未决待核边（含来源）
        self.resolved = []    # 已被签名裁决的待核边
        self.distinct = set()  # 已裁决异只的检测对
        self.deducted = 0     # 已签同只造成的基线扣减

    def add_frame(self, frame):
        verify_frame(frame)
        for det in frame['detections']:
            ref = frame['id'] + '#' + det['label']
            if ref in self.nodes:
                raise ValueError('duplicate detection ' + ref)
            self.nodes[ref] = {
                'frame': frame['id'],
                'camera': frame['camera'],
                'captured_at': int(frame['captured_at']),
                'label': det['label'],
                'overlaps': set(frame.get('overlaps', [frame['camera']])),
            }
            self.parent[ref] = ref

    def find(self, x):
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        self.parent[rb] = ra
        return True

    def _check_ref(self, ref):
        if ref not in self.nodes:
            raise ValueError('unknown detection ' + ref)

    def cannot_link(self, a, b):
        """同一帧内，或同一时刻两台视野不重叠的相机，不能是同一只鹿。"""
        self._check_ref(a)
        self._check_ref(b)
        x, y = self.nodes[a], self.nodes[b]
        if x['frame'] == y['frame']:
            return True
        if x['captured_at'] == y['captured_at'] and x['camera'] != y['camera']:
            if y['camera'] not in x['overlaps'] and x['camera'] not in y['overlaps']:
                return True
        return False

    def add_edge(self, edge_id, left, right, status, basis, source=None):
        """增量加入一条边：确定边立即合并分量，待核边登记待复核并拉开区间。"""
        self._check_ref(left)
        self._check_ref(right)
        pair = frozenset((left, right))
        record = {'edge': edge_id, 'left': left, 'right': right,
                  'basis': basis, 'source': source or edge_id}
        if status == 'confirmed':
            if pair in self.distinct:
                raise ValueError('确定边与已裁决异只矛盾 ' + edge_id)
            if self.cannot_link(left, right):
                raise PhysicalConstraintError('确定边违反同一时刻不重叠视野约束 ' + edge_id)
            self.union(left, right)
            self.confirmed.append(record)
        elif status == 'pending':
            if pair in self.distinct:
                raise ValueError('待核边与已裁决异只矛盾 ' + edge_id)
            if self.cannot_link(left, right):
                raise PhysicalConstraintError('待核边违反同一时刻不重叠视野约束 ' + edge_id)
            self.pending.append(record)
        else:
            raise ValueError('unknown edge status ' + status)

    def apply_resolution(self, edge_id, verdict):
        """签名裁决一条待核边：同只则合并分量并扣减基线，异只则登记互斥。"""
        edge = next((e for e in self.pending if e['edge'] == edge_id), None)
        if edge is None:
            raise ValueError('no open pending edge ' + edge_id)
        self.pending.remove(edge)
        left, right = edge['left'], edge['right']
        pair = frozenset((left, right))
        if verdict == 'same':
            if pair in self.distinct:
                raise ValueError('签名与已裁决异只矛盾 ' + edge_id)
            if self.cannot_link(left, right):
                raise PhysicalConstraintError('签名违反同一时刻不重叠视野约束 ' + edge_id)
            if self.union(left, right):
                self.deducted += 1
        elif verdict == 'distinct':
            if self.find(left) == self.find(right):
                raise ValueError('签名与已确定同只矛盾 ' + edge_id)
            self.distinct.add(pair)
        else:
            raise ValueError('unknown verdict ' + verdict)
        self.resolved.append({'edge': edge_id, 'verdict': verdict,
                              'left': left, 'right': right, 'basis': edge['basis']})

    def components(self):
        groups = {}
        for ref in self.nodes:
            groups.setdefault(self.find(ref), []).append(ref)
        return groups

    def open_pending(self):
        """仍跨不同连通分量的未决待核边。"""
        return [e for e in self.pending if self.find(e['left']) != self.find(e['right'])]

    def bounds(self):
        """可辨识总数区间：[基线 - 待核边数 - 已签同只数, 基线 - 已签同只数]。"""
        raw = len(self.nodes)
        return raw - len(self.open_pending()) - self.deducted, raw - self.deducted


def _valid_signatures(signatures):
    """按时间序应用撤销后仍有效的签名（纯撤销事件本身不作为裁决）。"""
    ordered = sorted(signatures, key=lambda s: (s['event_time'], s['received_at'], s['id']))
    revoked = set()
    for sig in ordered:
        target = sig['data'].get('revokes')
        if target:
            revoked.add(target)
    return [s for s in ordered if s['id'] not in revoked]


def resolve_verdicts(edges, signatures):
    """有效签名对每条待核边的最新裁决：edge_id -> (verdict, signature)。"""
    pending_ids = {e['id'] for e in edges if e['data'].get('status') == 'pending'}
    verdicts = {}
    for sig in _valid_signatures(signatures):
        data = sig['data']
        if 'verdict' not in data:
            continue  # 纯撤销更正
        edge_id = data.get('edge')
        if edge_id not in pending_ids:
            raise ValueError('signature for non-pending edge ' + str(edge_id))
        if data['verdict'] not in VERDICT_TEXT:
            raise ValueError('unknown verdict ' + str(data['verdict']))
        verdicts[edge_id] = (data['verdict'], sig)
    return verdicts


def build_graph(frames, edges, signatures=()):
    """由帧、关联边与复核签名构建重识别图；返回 (图, 裁决)。"""
    graph = ReidGraph()
    for frame in sort_frames(frames):
        graph.add_frame(frame)
    edge_by_id = {}
    for edge in sorted(edges, key=lambda e: (e['event_time'], e['received_at'], e['id'])):
        data = edge['data']
        graph.add_edge(edge['id'], data['left'], data['right'],
                       data.get('status', 'pending'), data.get('basis', ''), source=edge['id'])
        edge_by_id[edge['id']] = data
    verdicts = resolve_verdicts(edges, signatures)
    for edge_id, (verdict, sig) in verdicts.items():
        data = sig['data']
        photo, day = data.get('photo'), data.get('photo_day')
        if not photo or day is None:
            raise ValueError('补证缺少同日耳标照片 ' + sig['id'])
        for ref in (edge_by_id[edge_id]['left'], edge_by_id[edge_id]['right']):
            if graph.nodes[ref]['captured_at'] // 1440 != int(day):
                raise ValueError('耳标照片非同日 ' + sig['id'])
        graph.apply_resolution(edge_id, verdict)
    return graph, verdicts


def decide(ev):
    """调查结论：区间普查与具签名精确普查两个版次化产物。"""
    frame_events = rows(ev, 'camera_frame')
    if not frame_events:
        return []
    frames = [dict(id=e['id'], **e['data']) for e in frame_events]
    edges = rows(ev, 'reid_edge')
    signatures = rows(ev, 'review_signature')
    graph, verdicts = build_graph(frames, edges, signatures)
    raw = len(graph.nodes)
    lower, upper = graph.bounds()
    open_edges = graph.open_pending()
    proof = ([e['id'] for e in frame_events] + [e['id'] for e in edges]
             + [s['id'] for s in _valid_signatures(signatures)])
    interval = {
        'protocol': '审慎区间规程',
        'unit': '只',
        'raw_detections': raw,
        'lower': lower,
        'upper': upper,
        'components': len(graph.components()),
        'confirmed': [{'edge': e['edge'], 'pair': [e['left'], e['right']], 'basis': e['basis']}
                      for e in graph.confirmed],
        'pending': [{'edge': e['edge'], 'pair': [e['left'], e['right']], 'basis': e['basis']}
                    for e in open_edges],
        'resolved': [{'edge': r['edge'], 'verdict': VERDICT_TEXT[r['verdict']]}
                     for r in graph.resolved],
    }
    if open_edges:
        precise = {
            'protocol': '现场补证规程',
            'unit': '只',
            'status': '待复核',
            'open': [e['edge'] for e in open_edges],
        }
    else:
        precise = {
            'protocol': '现场补证规程',
            'unit': '只',
            'status': '已签名',
            'total': raw - graph.deducted,
            'resolutions': [
                {'edge': edge_id,
                 'verdict': VERDICT_TEXT[verdict],
                 'signature': sig['id'],
                 'reviewer': sig['data'].get('reviewer'),
                 'photo': sig['data'].get('photo')}
                for edge_id, (verdict, sig) in sorted(verdicts.items())
            ],
        }
    return [(INTERVAL_SURFACE, interval, proof), (PRECISE_SURFACE, precise, proof)]
