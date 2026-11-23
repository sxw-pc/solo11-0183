from .water_meter import (
    TOLERANCE,
    delta,
    meter_volume,
    verify_event,
    unsigned_or_bad_readings,
)
from .common import rows


def used(readings):
    """相邻差求和（题10基线：[100,105,110] -> 10）。"""
    return sum(delta(a, b) for a, b in zip(readings, readings[1:]))

# 有向水量账：每条流水在管网中拥有唯一 edge_id 与方向（from->to），
# 边缘体积只来自对应仪表的验签读数；内部回流是边，不是新的外部取水。

INFRASTRUCTURE = 'infrastructure'          # 基础设施维护协议：管理方承担全部漏损
JOINT_STEWARDSHIP = 'joint_stewardship'    # 共同管护协议：漏损按真实饮用量分摊

# 终端角色：水在该节点被实耗或渗入地下，只进不出，仪表读数本身即终端凭证
TERMINAL_ROLES = {'consumption', 'main_leak', 'filter_loss'}


def _f(value):
    return round(float(value), 6)


def build_pipes(events):
    """管道方向事件定义边；同一 edge_id 的重复定义必须一致，否则拒绝。"""
    pipes = {}
    for event in sorted(events, key=lambda e: (e['event_time'], e.get('received_at', 0), e['id'])):
        if event.get('kind') != 'pipe':
            continue
        data = event['data']
        edge_id = data['edge_id']
        record = dict(edge_id=edge_id, source=data['from'], target=data['to'],
                      meter_id=data['meter_id'], role=data['role'],
                      holder=data.get('holder'), event_id=event['id'])
        if edge_id in pipes:
            old = {k: v for k, v in pipes[edge_id].items() if k != 'event_id'}
            new = {k: v for k, v in record.items() if k != 'event_id'}
            if old != new:
                raise ValueError('conflicting pipe direction ' + edge_id)
            continue
        pipes[edge_id] = record
    return pipes


def valve_states(events):
    """阀门动作按 (event_time, received_at, id) 取每边最后一条；open 缺省为 True。"""
    states = {}
    for event in sorted(events, key=lambda e: (e['event_time'], e.get('received_at', 0), e['id'])):
        if event.get('kind') != 'valve_action':
            continue
        data = event['data']
        states[data['edge_id']] = dict(open=bool(data.get('open', True)),
                                       event_id=event['id'],
                                       event_time=event['event_time'])
    return states


def edge_volumes(events, pipes):
    """每条边的有向体积 = 其绑定仪表的校准后相邻差之和；无读数的边体积为 None。"""
    volumes = {}
    for edge_id, pipe in pipes.items():
        reading_ids = [e['id'] for e in events
                       if e.get('kind') == 'meter_reading' and e['data'].get('meter_id') == pipe['meter_id']]
        if not reading_ids:
            volumes[edge_id] = None
        else:
            volumes[edge_id] = _f(meter_volume(events, pipe['meter_id']))
    return volumes


def _nodes(pipes):
    nodes = {}
    for pipe in pipes.values():
        nodes.setdefault(pipe['source'], set()).add(pipe['edge_id'])
        nodes.setdefault(pipe['target'], set()).add(pipe['edge_id'])
    return nodes


def reservoir_change(events, node_id):
    """蓄水池前后水位：增量 = (after-before)*area，取最新一组闭合水位证据。"""
    level = None
    for event in sorted(events, key=lambda e: (e['event_time'], e.get('received_at', 0), e['id'])):
        if event.get('kind') != 'reservoir_level' or event['data'].get('node') != node_id:
            continue
        data = event['data']
        area = float(data.get('area', 1.0))
        level = dict(increase=_f((float(data['level_after']) - float(data['level_before'])) * area),
                     event_id=event['id'],
                     signed=verify_event(event))
    return level


def _bad_meter_for_edge(events, meter_id):
    bad = unsigned_or_bad_readings([e for e in events if e.get('data', {}).get('meter_id') == meter_id])
    return bad


def reconcile(events, tolerance=TOLERANCE):
    """
    逐节点闭合管网。返回：
      edges: 每边体积与仪表
      nodes: 每节点守恒差额、关联仪表、状态（closed 表示差额超容差被冻结）
      frozen: 被冻结节点列表
      reservoir: 水位核对结果
    冻结只冻结该节点及其下游牧户凭证，绝不把差额摊给所有牧户。
    """
    pipes = build_pipes(events)
    volumes = edge_volumes(events, pipes)
    valves = valve_states(events)
    node_edges = _nodes(pipes)

    reservoir_nodes = {e['data'].get('node') for e in events if e.get('kind') == 'reservoir_level'}
    source_roles = {'intake'}

    result_edges = {}
    for edge_id, pipe in pipes.items():
        result_edges[edge_id] = dict(pipe=pipe, volume=volumes[edge_id],
                                     valve=valves.get(edge_id, {}).get('open', True))

    nodes = {}
    for node, incident in sorted(node_edges.items()):
        inflow, outflow = [], []
        meters, bad_readings = [], []
        reasons = []
        for edge_id in sorted(incident):
            pipe = pipes[edge_id]
            meter_id = pipe['meter_id']
            meters.append(meter_id)
            bad = _bad_meter_for_edge(events, meter_id)
            if bad:
                bad_readings.extend(bad)
            volume = volumes[edge_id]
            entry = (edge_id, meter_id, volume)
            if pipe['target'] == node:
                inflow.append(entry)
            if pipe['source'] == node:
                outflow.append(entry)
            # 阀门关闭却仍有量，或边缺少任何读数，都是冻结理由
            valve = valves.get(edge_id)
            if valve is not None and not valve['open'] and (volume or 0) > tolerance:
                reasons.append('valve_closed_but_flowing:' + edge_id)
            if volume is None:
                reasons.append('meter_missing:' + edge_id)

        in_sum = sum(v for _, _, v in inflow if v is not None)
        out_sum = sum(v for _, _, v in outflow if v is not None)

        if any(pipe['role'] in source_roles for pipe in (pipes[e] for e in incident)):
            intake = sum(v for e, _, v in outflow if pipes[e]['role'] in source_roles and v is not None)
            discrepancy = _f(in_sum + intake - out_sum)
            kind = 'source'
        elif node in reservoir_nodes:
            level = reservoir_change(events, node)
            storage = level['increase'] if level else 0.0
            if level is not None and not level['signed']:
                reasons.append('unsigned_reservoir_level')
            discrepancy = _f(in_sum - out_sum - storage)
            kind = 'reservoir'
        elif not outflow and all(pipes[e]['role'] in TERMINAL_ROLES for e, _, _ in inflow):
            # 终端汇（饮水点/渗漏地面）：单条入流边即终点，终端仪表读数本身就是合法终止量，
            # 没有第二只独立表可交叉核对，故结构上守恒差额为0；
            # 该节点仅在入流边缺表、读数验签失败或阀门异常时经 reasons 冻结。
            # 终端表的多走/少记会在上游具备独立入出流仪表的 junction/source 节点暴露差额。
            discrepancy = 0.0
            kind = 'sink'
        else:
            discrepancy = _f(in_sum - out_sum)
            kind = 'junction'

        if bad_readings:
            reasons.append('bad_reading_signature:' + ','.join(sorted(set(bad_readings))))
        frozen = bool(reasons) or abs(discrepancy) > tolerance
        if not reasons and abs(discrepancy) > tolerance:
            reasons.append('imbalance_exceeds_tolerance')

        nodes[node] = dict(kind=kind,
                           inflow=[dict(edge_id=e, meter_id=m, volume=v) for e, m, v in inflow],
                           outflow=[dict(edge_id=e, meter_id=m, volume=v) for e, m, v in outflow],
                           discrepancy=discrepancy,
                           meters=sorted(set(meters)),
                           frozen=frozen,
                           reasons=sorted(set(reasons)))

    reservoir = {n: reservoir_change(events, n) for n in sorted(reservoir_nodes)}
    frozen = sorted(n for n, info in nodes.items() if info['frozen'])
    return dict(edges=result_edges, nodes=nodes, frozen=frozen, reservoir=reservoir,
                tolerance=tolerance)


def totals(events, reconciliation=None):
    """闭合口径下的全局水量账：外部进水 = 实耗 + 蓄水增量 + 有证漏损。"""
    reconciliation = reconciliation or reconcile(events)
    pipes = build_pipes(events)
    volumes = edge_volumes(events, pipes)

    def role_sum(roles):
        return _f(sum(v for e, pipe in pipes.items() if pipe['role'] in roles for v in [volumes[e]] if v is not None))

    external_in = role_sum({'intake'})
    consumed = role_sum({'consumption'})
    storage = role_sum({'direct_storage', 'filter_storage'})
    leakage = role_sum({'main_leak', 'filter_loss'})
    internal_return = role_sum({'return'})
    by_holder = {}
    for edge_id, pipe in pipes.items():
        if pipe['role'] == 'consumption' and pipe['holder']:
            by_holder[pipe['holder']] = _f(by_holder.get(pipe['holder'], 0) + (volumes[edge_id] or 0))
    level_storage = None
    for info in reconciliation['reservoir'].values():
        if info is not None:
            level_storage = info['increase']
    return dict(external_intake=external_in,
                consumed=consumed,
                storage_increase=storage,
                leakage=leakage,
                internal_return=internal_return,
                consumed_by_holder=by_holder,
                reservoir_level_increase=level_storage,
                closed=_f(consumed + storage + leakage))


# ---- 题目要求的两个 120 反例：常规采集全对，但图语义错误 ----

def naive_return_as_new_intake(events):
    """把内部回流当作第二次新取水：100 进水 + 20 回流 = 120。"""
    pipes = build_pipes(events)
    volumes = edge_volumes(events, pipes)
    intake = sum(v for e, p in pipes.items() if p['role'] == 'intake' for v in [volumes[e]] if v is not None)
    returned = sum(v for e, p in pipes.items() if p['role'] == 'return' for v in [volumes[e]] if v is not None)
    return _f(intake + returned)


def naive_branch_fully_booked(events):
    """
    错误口径：主管首轮分配的整额全部当实耗（A=40、B支路=50 都不减回流），
    滤池产出仍作为外部蓄水产品入账：首轮 40+50，再加直入5、滤池18、漏损5+2 = 120。
    """
    pipes = build_pipes(events)
    volumes = edge_volumes(events, pipes)
    intake_edge = next(edge_id for edge_id, pipe in pipes.items() if pipe['role'] == 'intake')
    header = pipes[intake_edge]['target']
    first_round = sum(volumes[e] or 0 for e, p in pipes.items() if p['source'] == header)
    downstream = sum(volumes[e] or 0 for e, p in pipes.items()
                     if p['role'] in ('filter_storage', 'filter_loss'))
    return _f(first_round + downstream)


def return_path_present(events):
    """回流表不可删：20->18+2 是定位滤池损耗的证据链。"""
    pipes = build_pipes(events)
    return any(pipe['role'] == 'return' for pipe in pipes.values())


# ---- 迟到证据的影响域：只重算接触到的节点，不触碰无关节点 ----

def impacted_nodes(events, touched_event_ids):
    pipes = build_pipes(events)
    edge_index = {}
    for edge_id, pipe in pipes.items():
        edge_index[edge_id] = {pipe['source'], pipe['target']}
    touched = set(touched_event_ids)
    nodes = set()
    for event in events:
        if event['id'] not in touched:
            continue
        data = event.get('data', {})
        if event.get('kind') in ('pipe', 'valve_action') and data.get('edge_id') in edge_index:
            nodes |= edge_index[data['edge_id']]
        elif event.get('kind') == 'meter_reading':
            for edge_id, pipe in pipes.items():
                if pipe['meter_id'] == data.get('meter_id'):
                    nodes |= edge_index[edge_id]
        elif event.get('kind') in ('meter_replacement', 'calibration'):
            for edge_id, pipe in pipes.items():
                if pipe['meter_id'] == data.get('meter_id'):
                    nodes |= edge_index[edge_id]
        elif event.get('kind') == 'reservoir_level':
            nodes.add(data.get('node'))
    return sorted(nodes)


# ---- 两种经签认的漏损归责凭证 ----

def _holder_nodes(pipes, holder):
    nodes = set()
    for pipe in pipes.values():
        if pipe['holder'] == holder:
            nodes.add(pipe['source'])
            nodes.add(pipe['target'])
    return nodes


def _blocked_holders(pipes, frozen):
    frozen = set(frozen)
    blocked = {}
    holders = sorted({p['holder'] for p in pipes.values() if p['holder']})
    for holder in holders:
        hits = sorted(_holder_nodes(pipes, holder) & frozen)
        if hits:
            blocked[holder] = hits
    return blocked


def signoffs(events):
    out = {}
    for event in rows(events, 'liability_signoff'):
        data = event['data']
        if not data.get('active', True):
            continue
        protocol = data['protocol']
        old = out.get(protocol)
        if old is None or (event['event_time'], event.get('received_at', 0), event['id']) > (
                old['event_time'], old.get('received_at', 0), old['id']):
            out[protocol] = event
    return out


def _holder_edge_reading_ids(events, pipes, holder):
    meters = {p['meter_id'] for p in pipes.values() if p['holder'] == holder}
    return sorted(e['id'] for e in events
                  if e.get('kind') == 'meter_reading' and e['data'].get('meter_id') in meters)


def issue_vouchers(events, reconciliation=None, protocol=None):
    """
    同一闭合物理账按签认协议出具凭证。物理实耗与总进水在两种协议下都是同一100，
    改变的只是收费体积、漏损责任与补水预算；无签认则不出具。
    """
    reconciliation = reconciliation or reconcile(events)
    pipes = build_pipes(events)
    volumes = edge_volumes(events, pipes)
    t = totals(events, reconciliation)
    blocked = _blocked_holders(pipes, reconciliation['frozen'])
    signed = signoffs(events)
    protocols = [protocol] if protocol else [INFRASTRUCTURE, JOINT_STEWARDSHIP]

    vouchers = {}
    for proto in protocols:
        signoff = signed.get(proto)
        if signoff is None:
            vouchers[proto] = dict(issued=False, reason='missing_signed_protocol', bills={})
            continue
        bills = {}
        consumption = t['consumed_by_holder']
        holders = sorted(consumption)
        # 被冻结牧户的水量不能进入其他牧户的漏损分摊分母
        effective_consumed = _f(sum(v for h, v in consumption.items() if h not in blocked))
        for holder in holders:
            actual = _f(consumption[holder])
            proof_ids = _holder_edge_reading_ids(events, pipes, holder) + [signoff['id']]
            if holder in blocked:
                bills[holder] = dict(issued=False,
                                     actual_consumption=actual,
                                     billed_volume=None,
                                     leakage_share=None,
                                     replenishment_budget=None,
                                     frozen_nodes=blocked[holder],
                                     evidence_ids=sorted(set(proof_ids)))
                continue
            if proto == INFRASTRUCTURE:
                leakage_share = 0.0
                billed = actual
                manager_fee_leakage = t['leakage']
            else:
                leakage_share = _f(t['leakage'] * actual / effective_consumed) if effective_consumed else 0.0
                billed = _f(actual + leakage_share)
                manager_fee_leakage = 0.0
            bills[holder] = dict(issued=True,
                                 actual_consumption=actual,
                                 billed_volume=billed,
                                 leakage_share=leakage_share,
                                 replenishment_budget=billed,
                                 frozen_nodes=[],
                                 evidence_ids=sorted(set(proof_ids)))
        manager = dict(physical_leakage_registered=t['leakage'],
                       fee_bearing_leakage=_f(t['leakage'] - sum(
                           b['leakage_share'] or 0 for b in bills.values())),
                       protocol_event_id=signoff['id'])
        vouchers[proto] = dict(issued=True,
                               protocol=proto,
                               signoff_event_id=signoff['id'],
                               signers=list(signoff['data'].get('signers', [])),
                               physical=dict(external_intake=t['external_intake'],
                                             consumed=t['consumed'],
                                             storage_increase=t['storage_increase'],
                                             leakage=t['leakage']),
                               bills=bills,
                               blocked_holders=sorted(blocked),
                               manager=manager)
    return vouchers
