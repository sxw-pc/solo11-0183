"""围栏报警诊断、隔离复测与维修工单。

诊断目标不是从报警里挑一套“最可能”的故障，而是枚举全部最小一致
故障集（最小击中集）：每个候选都能解释所有报警，且去掉任意一段
就不再能解释。互斥候选必须同时保留，频次/单点挑选都不能声称
证实了某一段。

两条授权规程读取同一份输入：
* 夜间安全规程（无需复测）：对并集内全部区段下预防检查工单，保留
  原始候选集合；
* 定位规程：仅当隔离复测通过 HMAC 验签时，依据复测把候选收敛到
  经证实的断线区段，下定位维修工单；缺签名或签名不符一律禁止。
"""

import hashlib
import hmac
import json

from .fence_sensor import failed_self_checks, tripped_loops

# 离线工作台内置的隔离复测授权密钥。真实部署由密钥管理注入。
RETEST_SECRET = b'rangeledger-fence-retest-v1'


class RetestSignatureError(ValueError):
    """隔离复测缺签名或签名不符；定位规程据此禁止执行。"""


def validate_topology(loops):
    """校验并规范化回路拓扑，返回 {回路: 排序去重后的区段列表}。

    每条回路必须覆盖至少一个已声明区段；区段重复在单条回路内去重。
    """
    if not isinstance(loops, dict) or not loops:
        raise ValueError('回路拓扑必须为非空映射')
    normalized = {}
    for loop, sections in loops.items():
        if not isinstance(loop, str) or not loop:
            raise ValueError('回路标识必须为非空字符串')
        if not isinstance(sections, (list, tuple, set)) or not sections:
            raise ValueError('回路 %s 必须覆盖至少一个区段' % loop)
        covered = sorted({s for s in sections if isinstance(s, str) and s})
        if not covered or len(covered) != len(list(sections)):
            raise ValueError('回路 %s 的区段列表非法或含重复/空区段' % loop)
        normalized[loop] = covered
    return normalized


def suspect_sections(readings, loops):
    """报警回路覆盖区段的并集（既有基线语义，去重并排序）。"""
    topology = validate_topology(loops)
    return sorted({section for loop in tripped_loops(readings)
                   for section in topology[loop]})


def minimal_fault_sets(tripped, loops):
    """枚举全部最小一致故障集。

    一个候选故障集与报警一致，当且仅当每条报警回路都至少有一段在
    候选内（击中集）；再剔除包含其它候选的非最小集。输出先按规模、
    再按字典序排序，保证确定性。报警为空时没有需要解释的回路，
    返回空集候选 ``[set()]``。
    """
    topology = validate_topology(loops)
    targets = sorted(set(tripped))
    unknown = [loop for loop in targets if loop not in topology]
    if unknown:
        raise ValueError('报警回路 %s 不在拓扑中' % unknown)
    if not targets:
        return [set()]
    universe = sorted({s for loop in targets for s in topology[loop]})

    def covers(candidate):
        return all(any(s in candidate for s in topology[loop]) for loop in targets)

    hitting = []
    for mask in range(1, 1 << len(universe)):
        candidate = {universe[i] for i in range(len(universe)) if mask & (1 << i)}
        if covers(candidate):
            hitting.append(candidate)
    minimal = []
    for candidate in hitting:
        if not any(other < candidate for other in hitting):
            minimal.append(candidate)
    return sorted(minimal, key=lambda c: (len(c), sorted(c)))


def alarm_explanation_edges(tripped, loops):
    """报警解释边：每条报警回路到其覆盖区段的 (loop, section) 边。

    这些边是“回路至少一段断线”这一约束的逐项展开，与最小集合枚举
    共用同一套校验后的拓扑。
    """
    topology = validate_topology(loops)
    edges = []
    for loop in sorted(set(tripped)):
        if loop not in topology:
            raise ValueError('报警回路 %s 不在拓扑中' % loop)
        for section in topology[loop]:
            edge = (loop, section)
            if edge not in edges:
                edges.append(edge)
    return edges


def _retest_payload(retest):
    if not isinstance(retest, dict):
        raise RetestSignatureError('隔离复测必须为对象')
    required = ('retest_id', 'isolated', 'branches')
    missing = [key for key in required if key not in retest]
    if missing:
        raise RetestSignatureError('隔离复测缺少字段 %s' % missing)
    return {key: retest[key] for key in required}


def retest_canonical(retest):
    """隔离复测参与签名的规范化字节串（不含签名字段本身）。"""
    return json.dumps(_retest_payload(retest), ensure_ascii=False,
                      sort_keys=True, separators=(',', ':')).encode('utf-8')


def sign_retest(retest, secret=RETEST_SECRET):
    """为隔离复测计算 HMAC-SHA256 十六进制签名。"""
    return hmac.new(secret, retest_canonical(retest), hashlib.sha256).hexdigest()


def verify_retest(retest, secret=RETEST_SECRET):
    """验签并返回复测对象；缺签名或不符抛 RetestSignatureError。"""
    payload = _retest_payload(retest)
    signature = retest.get('signature')
    if not isinstance(signature, str) or not signature:
        raise RetestSignatureError('隔离复测 %s 缺少授权签名，禁止定位'
                                   % payload['retest_id'])
    expected = sign_retest(payload, secret)
    if not hmac.compare_digest(signature, expected):
        raise RetestSignatureError('隔离复测 %s 签名不符，禁止定位'
                                   % payload['retest_id'])
    return retest


def retest_proven_breaks(retest, loops):
    """解释已验签复测，返回被证实断线的区段集合。

    ``isolated`` 区段从其所属回路切离；切离后仍报警的支路必然还含有
    断线段。只剩一个区段的支路报警即直接证实该区段断线；多区段支路
    报警只在其单段兄弟支路已证伪时才由“必有一段断线”补证。
    """
    topology = validate_topology(loops)
    payload = _retest_payload(retest)
    isolated = payload['isolated']
    branches = payload['branches']
    if not isinstance(branches, list) or not branches:
        raise RetestSignatureError('隔离复测支路观测为空')
    proven = set()
    observations = []
    for branch in branches:
        if not isinstance(branch, dict) or 'sections' not in branch or 'alarm' not in branch:
            raise RetestSignatureError('隔离复测支路观测格式非法')
        sections = sorted(set(branch['sections']))
        if not sections:
            raise RetestSignatureError('隔离复测支路不能为空')
        if isolated in sections:
            raise RetestSignatureError('支路 %s 仍含已切离区段 %s'
                                       % (sections, isolated))
        for section in sections:
            if not any(section in covered for covered in topology.values()):
                raise RetestSignatureError('复测支路区段 %s 不在拓扑中' % section)
        observations.append((sections, bool(branch['alarm'])))
        if branch['alarm'] and len(sections) == 1:
            proven.add(sections[0])
    # 多段报警支路：排除已证实完好的单段支路后，若只剩一个未排除区段则补证。
    intact = {sections[0] for sections, alarm in observations
              if not alarm and len(sections) == 1}
    for sections, alarm in observations:
        if alarm and len(sections) > 1:
            remaining = [s for s in sections if s not in intact and s not in proven]
            if len(remaining) == 1:
                proven.add(remaining[0])
    return proven


def night_safety_procedure(candidates, evidence_ids):
    """夜间安全规程：并集内全部区段预防检查，候选原样保留。"""
    sections = sorted({s for c in candidates for s in c})
    orders = [{
        'order_id': 'WO-night-%s' % section,
        'kind': '预防检查工单',
        'section': section,
        'basis': '夜间安全规程：候选未排除，预防性巡查',
    } for section in sections]
    return {
        'status': '执行',
        'candidates': [sorted(c) for c in candidates],
        'work_orders': orders,
        'evidence_ids': sorted(set(evidence_ids)),
    }


def location_procedure(candidates, retest, loops, evidence_ids):
    """定位规程：验签复测 → 收敛候选 → 仅对证实断线段下维修工单。

    复测缺签名/签名不符时抛 RetestSignatureError，调用方负责按禁止
    处理；任何情况下都不能在未验签时声称区段已证实。
    """
    verified = verify_retest(retest)
    proven = retest_proven_breaks(verified, loops)
    # 复测证伪：候选必须包含全部证实断线段，且不得含证实完好的区段。
    intact = set()
    for branch in verified['branches']:
        if not branch['alarm'] and len(branch['sections']) == 1:
            intact.add(branch['sections'][0])
    confirmed = [c for c in candidates
                 if c and proven.issubset(c) and not (c & intact)]
    if not confirmed:
        raise RetestSignatureError('隔离复测结论与报警候选矛盾，禁止定位')
    orders = [{
        'order_id': 'WO-locate-%s' % section,
        'kind': '定位维修工单',
        'section': section,
        'basis': '定位规程：隔离复测证实该区段断线',
    } for section in sorted(proven)]
    return {
        'status': '定位执行',
        'retest_id': verified['retest_id'],
        'candidates': [sorted(c) for c in confirmed],
        'proven_broken': sorted(proven),
        'work_orders': orders,
        'evidence_ids': sorted(set(evidence_ids)),
    }


def decide(ev):
    """把围栏证据事件流转为四项版本化决定，接口与其余子系统一致。

    返回 (决定名, 结果, 证据ID列表) 三元组列表；定位规程在复测缺失
    或验签失败时输出“禁止”而不是抛错，保证证据重放不中断。
    """
    topo_events = [e for e in ev if e['kind'] == 'fence_topology']
    alarm_events = [e for e in ev if e['kind'] == 'fence_alarm']
    retest_events = [e for e in ev if e['kind'] == 'fence_retest']
    if not topo_events:
        return [('围栏最小故障集诊断', {'status': '缺拓扑', 'candidates': []},
                 [e['id'] for e in ev])]
    topo_event = max(topo_events, key=lambda e: (e['event_time'], e['received_at'], e['id']))
    topology = validate_topology(topo_event['data']['loops'])
    readings = [r for e in sorted(alarm_events, key=lambda e: (e['event_time'], e['id']))
                for r in e['data'].get('readings', [])]
    tripped = tripped_loops(readings)
    failed = failed_self_checks(readings)
    if failed:
        raise ValueError('传感器自检失败，报警不可信：%s' % failed)
    candidates = minimal_fault_sets(tripped, topology)
    edges = alarm_explanation_edges(tripped, topology)
    proof_ids = [topo_event['id']] + [e['id'] for e in alarm_events]
    night = night_safety_procedure(candidates, proof_ids)
    if not retest_events:
        location = {
            'status': '禁止',
            'reason': '缺少已签名隔离复测',
            'candidates': [sorted(c) for c in candidates],
            'work_orders': [],
            'evidence_ids': sorted(set(proof_ids)),
        }
    else:
        retest_event = max(retest_events,
                           key=lambda e: (e['event_time'], e['received_at'], e['id']))
        try:
            location = location_procedure(
                candidates, retest_event['data'], topology,
                proof_ids + [retest_event['id']])
        except RetestSignatureError as exc:
            location = {
                'status': '禁止',
                'reason': str(exc),
                'candidates': [sorted(c) for c in candidates],
                'work_orders': [],
                'evidence_ids': sorted(set(proof_ids + [retest_event['id']])),
            }
    return [
        ('围栏最小故障集诊断', {
            'tripped_loops': tripped,
            'candidates': [sorted(c) for c in candidates],
        }, proof_ids or [e['id'] for e in ev]),
        ('围栏报警解释边', [
            {'loop': loop, 'section': section} for loop, section in edges
        ], proof_ids or [e['id'] for e in ev]),
        ('夜间安全预防工单', night, proof_ids or [e['id'] for e in ev]),
        ('隔离定位维修工单', location, location['evidence_ids']),
    ]
