"""跨牧户旱季预约的全局分配器。

输入全部来自离线证据事件（booking / zone_capacity / boundary / permit /
vegetation_sample / herd / quarantine / movement / rest_order），输出可审计的
当班分配表，并把已放行的预约投影成带 alloc_version 的 movement 事件，
再由 permit / movement / recovery 三个既有入口复核。
"""
from pathlib import Path
from .common import rows, proof, pressure, capacity

COMMAND = 'schedule'
KINDS = ['pasture', 'permit', 'allocation', 'herd', 'movement', 'quarantine',
         'vegetation_sample', 'rest_order', 'compensation',
         'booking', 'zone_capacity', 'boundary']
ROOT = Path(__file__).resolve().parent.parent
NORMAL = str(ROOT / 'fixtures' / 'schedule_synth_exchange.json')
FAILURE = str(ROOT / 'fixtures' / 'schedule_synth_locked.json')

GRASS_FLOOR = 0.5          # 草量低于该比例的草区不开放
SURFACE_INTAKE = '预约受理台账'
SURFACE_PERMIT = '预约许可校验'
SURFACE_CAP = '分区草量容量快照'
SURFACE_GREEDY = '局部贪心分配'
SURFACE_GLOBAL = '全局交换分配'
SURFACE_PROTOCOL = '签认协议选择'
SURFACE_TABLE = '当班全局分配表'
SURFACE_BLOCKED = '被阻挡约束台账'
SURFACE_PROJECTION = 'movement投影核对'


def _denied_zones(ev, herd):
    out = set()
    for b in rows(ev, 'boundary'):
        d = b['data']
        if d.get('kind') == 'herd_deny' and d.get('herd') == herd:
            out.add(d.get('zone'))
    return out


def _corridor_busy(zone, start, end, placements):
    """通道/草区不能同窗共享：同区已有占用且时段相交即冲突。"""
    for p in placements:
        if p['zone'] == zone and p['start'] < end and start < p['end']:
            return True
    return False


def _over_capacity(zone, demand, placements, snap):
    used = sum(p['demand'] for p in placements if p['zone'] == zone)
    return used + demand > snap[zone]['capacity'] + 1e-9


def intake_bookings(ev):
    """按到达次序回放预约申请，处理重复、取消、改期与同群跨区冲突。"""
    events = sorted(rows(ev, 'booking'),
                    key=lambda e: (e['received_at'], e['event_time'], e['id']))
    active, ledger, anomalies = {}, [], []
    for e in events:
        d = e['data']
        key = (d.get('herd'), d.get('start'), d.get('end'))
        item = dict(id=e['id'], received_at=e['received_at'],
                    event_time=e['event_time'], herd=d.get('herd'),
                    holder=d.get('holder'), start=float(d.get('start', 0)),
                    end=float(d.get('end', 0)), count=int(d.get('count', 0)),
                    zones=list(d.get('zones', [])),
                    action=d.get('action', 'book'))
        if item['action'] == 'cancel':
            if key in active:
                item['status'] = 'cancelled'
                active.pop(key)
            else:
                item['status'] = 'orphan_cancel'
                anomalies.append(('取消先于申请到达', e['id'], key))
            ledger.append(item)
            continue
        if d.get('reschedule_of'):
            prior = d['reschedule_of']
            item['status'] = 'rescheduled'
            item['reschedule_of'] = prior
            # 原预约无论是否已入场都保留为可回放的历史版本，只做状态标记，
            # 并从待分配集合中移除，避免旧窗口与改期窗口重复入场。
            for old_key, old in list(active.items()):
                if old['id'] == prior:
                    active.pop(old_key)
            for old in ledger:
                if old['id'] == prior and old['status'] in ('booked', 'admitted'):
                    old['status'] = 'superseded_by_reschedule'
            active[key] = item
            ledger.append(item)
            continue
        conflict = next((o for o in active.values()
                         if o['herd'] == item['herd']
                         and o['start'] < item['end'] and item['start'] < o['end']
                         and set(o['zones']) != set(item['zones'])), None)
        if conflict:
            item['status'] = 'cross_zone_conflict'
            item['conflict_with'] = conflict['id']
            anomalies.append(('同群跨区冲突', e['id'], conflict['id']))
            ledger.append(item)
            continue
        if key in active:
            item['status'] = 'duplicate'
            item['duplicate_of'] = active[key]['id']
            anomalies.append(('重复预约', e['id'], active[key]['id']))
            ledger.append(item)
            continue
        item['status'] = 'booked'
        active[key] = item
        ledger.append(item)
    return active, ledger, anomalies


def _permit_for(ev, herd, holder):
    permits = [p for p in rows(ev, 'permit')
               if p['data'].get('herd', p['data'].get('parcel')) is not None]
    scoped = [p for p in permits
              if p['data'].get('herd') == herd and p['data'].get('holder', 'H1') == holder]
    pool = scoped or permits
    return max(pool, key=lambda p: (p['event_time'], p['received_at'], p['id']),
               default=None)


def _signing_basis(permit):
    if not permit:
        return dict(protocol='none', locked_zone=None, exchangeable=False,
                    reason='缺少许可签认证据', proof=[])
    d = permit['data']
    signatures = set(d.get('signatures', []) or [])
    locked = d.get('locked_zone')
    if locked and {'holder', 'vet'} <= signatures:
        return dict(protocol='locked', locked_zone=locked, exchangeable=False,
                    reason='牧户与防疫员双签锁区，当天不允许自动转签',
                    proof=[permit['id']])
    return dict(protocol='exchangeable', locked_zone=None, exchangeable=True,
                reason='仅签时间未锁区，调度协议可交换草区',
                proof=[permit['id']])


def zone_snapshot(ev):
    caps = {c['data'].get('zone'): c for c in rows(ev, 'zone_capacity')}
    samples = {}
    for s in rows(ev, 'vegetation_sample'):
        z = s['data'].get('zone', 'P1')
        if z not in samples or (s['event_time'], s['received_at'], s['id']) > \
                (samples[z]['event_time'], samples[z]['received_at'], samples[z]['id']):
            samples[z] = s
    snap = {}
    for zone, c in caps.items():
        s = samples.get(zone)
        grass = float(s['data'].get('grass', 1)) if s else None
        cap = float(c['data'].get('capacity', c['data'].get('quota', 0)))
        # 草量样方未补录的草区不得参与分配：容量证据不等于开放证据。
        snap[zone] = dict(capacity=cap, grass=grass,
                          open=grass is not None and grass >= GRASS_FLOOR,
                          evidence=[c['id']] + ([s['id']] if s else []))
    return snap, caps, samples


def _build_rows(ev, active):
    snap, caps, samples = zone_snapshot(ev)
    built = []
    for item in sorted(active.values(),
                       key=lambda x: (x['received_at'], x['event_time'], x['id'])):
        permit = _permit_for(ev, item['herd'], item['holder'])
        basis = _signing_basis(permit)
        permit_ok = bool(permit) and float(permit['data'].get('start', -1e12)) <= item['start'] \
            and item['end'] <= float(permit['data'].get('end', 1e12))
        denied = _denied_zones(ev, item['herd'])
        allowed, zone_reasons = [], {}
        # 申请草区之外的群级禁行边界也记录在案，说明“为何不能改派他区”。
        external_blocks = {z: '群级禁行边界（接种留观）' for z in denied
                           if z not in item['zones']}
        if not permit_ok:
            for z in item['zones']:
                zone_reasons[z] = '许可缺失或不在预约有效期内'
        for z in item['zones']:
            if z in zone_reasons:
                continue
            if z in denied:
                zone_reasons[z] = '群级禁行边界（接种留观）'
            elif basis['protocol'] == 'locked' and z != basis['locked_zone']:
                zone_reasons[z] = '锁区双签禁止自动转签'
            elif z not in snap:
                zone_reasons[z] = '缺少分区容量/草量证据'
            elif not snap[z]['open']:
                zone_reasons[z] = '草量低于开放阈值'
            else:
                allowed.append(z)
        demand = item['count'] * (item['end'] - item['start'])
        built.append(dict(item, permit_id=permit['id'] if permit else None,
                          permit_valid=permit_ok, signing=basis,
                          allowed_zones=allowed, zone_reasons=zone_reasons,
                          external_blocks=external_blocks, demand=demand))
    return built, snap


def _try_place(rows_in, snap, key):
    """按 key(row) 的草区次序做局部贪心：先到先派第一处同窗有余量的草区。"""
    placements, blocked = [], []
    for r in rows_in:
        placed = False
        for z in key(r):
            if z not in snap or not snap[z]['open']:
                continue
            if _corridor_busy(z, r['start'], r['end'], placements):
                continue
            if _over_capacity(z, r['demand'], placements, snap):
                continue
            placements.append(dict(booking_id=r['id'], herd=r['herd'],
                                   holder=r['holder'], zone=z,
                                   start=r['start'], end=r['end'],
                                   count=r['count'], demand=r['demand'],
                                   permit_id=r['permit_id']))
            placed = True
            break
        if not placed:
            why = r['zone_reasons'].copy()
            for z in r['allowed_zones']:
                if _corridor_busy(z, r['start'], r['end'], placements):
                    why[z] = '通道同窗共享冲突/容量已占满'
                elif _over_capacity(z, r['demand'], placements, snap):
                    why[z] = '分区动物小时容量不足'
            if not why:
                why = {z: '通道同窗共享冲突/容量已占满' for z in r['zones']}
            # 申请外草区的禁行/锁区约束一并归档，杜绝伪造他区许可。
            why.update(r.get('external_blocks', {}))
            blocked.append(dict(booking_id=r['id'], herd=r['herd'],
                                holder=r['holder'], reasons=why))
    return placements, blocked


def greedy(rows_in, snap):
    return _try_place(rows_in, snap, lambda r: r['zones'])


def _backtrack(rows_in, snap, idx, placements):
    if idx == len(rows_in):
        return list(placements)
    r = rows_in[idx]
    for z in r['allowed_zones']:
        if _corridor_busy(z, r['start'], r['end'], placements):
            continue
        if _over_capacity(z, r['demand'], placements, snap):
            continue
        placements.append(dict(booking_id=r['id'], herd=r['herd'],
                               holder=r['holder'], zone=z,
                               start=r['start'], end=r['end'],
                               count=r['count'], demand=r['demand'],
                               permit_id=r['permit_id']))
        found = _backtrack(rows_in, snap, idx + 1, placements)
        if found is not None:
            return found
        placements.pop()
    return None


def global_solve(rows_in, snap):
    """全局回溯：在完整输入上搜索让全部预约落地的可行交换分配。"""
    found = _backtrack(rows_in, snap, 0, [])
    if found is not None:
        return found, [], True
    placements, blocked = greedy(rows_in, snap)
    return placements, blocked, False


def decide(ev):
    if not rows(ev, 'booking'):
        return []
    active, ledger, anomalies = intake_bookings(ev)
    rows_in, snap = _build_rows(ev, active)

    permit_surface = {r['id']: dict(herd=r['herd'], permit_id=r['permit_id'],
                                    permit_valid=r['permit_valid'],
                                    protocol=r['signing']['protocol'])
                      for r in rows_in}
    g_place, g_block = greedy(rows_in, snap)
    p_place, p_block, feasible = global_solve(rows_in, snap)

    protocols = {r['herd']: r['signing'] for r in rows_in}
    exchangeable = all(r['signing']['exchangeable'] for r in rows_in)
    chosen_protocol = 'exchangeable' if exchangeable else 'locked'
    final_place, final_block = (p_place, p_block) if exchangeable \
        else (g_place, g_block)

    table = [dict(herd=p['herd'], holder=p['holder'], zone=p['zone'],
                  window=[p['start'], p['end']], count=p['count'],
                  animal_hours=p['demand'], permit_id=p['permit_id'],
                  booking_id=p['booking_id']) for p in final_place]
    deferred = [dict(herd=b['herd'], holder=b['holder'],
                     status='待改期', reasons=b['reasons'],
                     booking_id=b['booking_id']) for b in final_block]

    ids = proof(rows(ev, 'booking'), rows(ev, 'zone_capacity'),
                rows(ev, 'boundary'), rows(ev, 'permit'),
                rows(ev, 'vegetation_sample')) or proof(ev)
    return [
        (SURFACE_INTAKE, dict(ledger=ledger, anomalies=[
            dict(kind=k, event=e, ref=ref) for k, e, ref in anomalies]), ids),
        (SURFACE_PERMIT, permit_surface, ids),
        (SURFACE_CAP, {z: {k: v for k, v in d.items()} for z, d in snap.items()}, ids),
        (SURFACE_GREEDY, dict(feasible=not g_block, placements=g_place,
                              blocked=g_block), ids),
        (SURFACE_GLOBAL, dict(feasible=feasible, placements=p_place,
                              blocked=p_block), ids),
        (SURFACE_PROTOCOL, dict(selected=chosen_protocol, basis=protocols), ids),
        (SURFACE_BLOCKED, [dict(herd=b['herd'], holder=b['holder'],
                                reasons=b['reasons']) for b in final_block], ids),
        (SURFACE_TABLE, dict(admitted=table, deferred=deferred,
                             protocol=chosen_protocol), ids),
    ]


def projected_movements(ev, alloc_version):
    """把当班分配表投影成确定的 movement 事件（未入场的待改期不投影）。"""
    table = None
    for name, result, _ in decide(ev):
        if name == SURFACE_TABLE:
            table = result
    if table is None:
        return []
    out = []
    for row in table['admitted']:
        source = next((e for e in rows(ev, 'booking')
                       if e['id'] == row['booking_id']), None)
        base = source['event_time'] if source else 0
        out.append(dict(
            id=f"move-proj-v{alloc_version}-{row['herd']}-{row['zone']}",
            kind='movement',
            subject=source['subject'] if source else 'case-1',
            event_time=base,
            received_at=(source['received_at'] if source else 0) + alloc_version,
            data=dict(herd=row['herd'], holder=row['holder'], zone=row['zone'],
                      start=row['window'][0], end=row['window'][1],
                      count=row['count'], outside=False,
                      alloc_version=alloc_version,
                      source_booking=row['booking_id'],
                      source_permit=row['permit_id'])))
    return out


def project_evidence(ev, alloc_version):
    """证据 + 本版本投影 movement，供三个既有入口复核。"""
    ids = {e['id'] for e in ev}
    extra = [m for m in projected_movements(ev, alloc_version) if m['id'] not in ids]
    return ev + extra


def adjudicate(ev, alloc_version):
    """沿 permit→movement→recovery 三入口复核投影后的占用与恢复裁决。

    返回 (surfaces, triples)：surfaces 为名称→结果，triples 为带证据 ID 的
    (名称, 结果, 证据ID) 三元组，可直接交给 save_decisions 落库。
    """
    from . import permit, movement, recovery
    projected = project_evidence(ev, alloc_version)
    scoped = [e for e in projected
              if e['kind'] != 'movement'
              or e['data'].get('alloc_version') in (None, alloc_version)]
    surfaces, triples = {}, []
    for module in (permit, movement, recovery):
        for name, value, ids in module.decide(scoped):
            surfaces[name] = value
            triples.append((name, value, ids))
    holder_pressure, zone_pressure = pressure(scoped)
    cap, grass = capacity(scoped)
    check = dict(
        alloc_version=alloc_version,
        zones=zone_pressure, holders=holder_pressure,
        total_animal_hours=round(sum(zone_pressure.values()), 2),
        capacity=cap, grass=grass)
    surfaces[SURFACE_PROJECTION] = check
    check_ids = proof([e for e in scoped if e['kind'] in
                       ('movement', 'permit', 'vegetation_sample',
                        'zone_capacity', 'quarantine', 'rest_order')]) or proof(scoped)
    triples.append((SURFACE_PROJECTION, check, check_ids))
    return surfaces, triples
