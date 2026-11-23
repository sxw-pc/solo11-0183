import hashlib
import hmac
import json
from collections import defaultdict

# 离线水表证据处理：delta 永远是 after-before；
# 读数可验签、可校准、可换表归零；相邻差按表计去重、按时刻排序后求和。

SIGNING_KEY = b'rangeledger-water-loop-seed'
TOLERANCE = 1.0


def delta(before, after):
    """单次水表读数差：after-before，方向不允许被调用方翻转。"""
    return after - before


def _number(data, key, default=0.0):
    value = data.get(key, default)
    return float(value) if value is not None else float(default)


def _canonical(event):
    """
    不含签名的稳定消息串。必须与 store 入库的 JSON 口径一致
    （sort_keys、ensure_ascii=False），这样证据经 SQLite 重放后验签仍然成立。
    """
    head = {k: event[k] for k in ('id', 'kind', 'subject', 'event_time', 'received_at')}
    data = {k: v for k, v in event.get('data', {}).items() if k != 'sig'}
    return json.dumps([head, data], ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def sign_event(event, key=SIGNING_KEY):
    """对单条离线证据签名，签名写入 data.sig，返回同一事件。"""
    event.setdefault('data', {})
    event['data'] = {k: v for k, v in event['data'].items() if k != 'sig'}
    event['data']['sig'] = hmac.new(key, _canonical(event).encode('utf-8'), hashlib.sha256).hexdigest()
    return event


def verify_event(event, key=SIGNING_KEY):
    """验签：未携带签名或签名不匹配均判 False，原始证据不被改写。"""
    sig = event.get('data', {}).get('sig')
    if not sig:
        return False
    expected = hmac.new(key, _canonical(event).encode('utf-8'), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(sig))


def verify_events(events, key=SIGNING_KEY):
    return {event['id']: verify_event(event, key) for event in events}


def calibration_factors(events, meter_id):
    """校准记录按 (event_time, received_at, id) 排序，取每条的 factor（默认1.0）。"""
    records = []
    for event in events:
        if event.get('kind') != 'calibration' or event.get('data', {}).get('meter_id') != meter_id:
            continue
        records.append((event['event_time'], event.get('received_at', 0), event['id'],
                        _number(event['data'], 'factor', 1.0)))
    records.sort()
    return records


def _factor_at(records, event_time):
    factor = 1.0
    for t, _r, _i, value in records:
        if t <= event_time:
            factor = value
        else:
            break
    return factor


def meter_deltas(events, meter_id):
    """
    单只表的可重放读数序列：
    - 只采信验签通过的 meter_reading；
    - 按 (event_time, received_at, id) 排序；
    - 同一 (event_time, reading) 去重（离线补传重复包）；
    - 换表/归零事件后的首读数以其 prior_reading 为前值，而不是沿用上一只表的末值。
    返回 [(event_time, raw_delta, calibrated_delta, reading_event_id), ...]。
    """
    records = [e for e in events
               if e.get('kind') == 'meter_reading'
               and e.get('data', {}).get('meter_id') == meter_id
               and verify_event(e)]
    records.sort(key=lambda e: (e['event_time'], e.get('received_at', 0), e['id']))
    factors = calibration_factors(events, meter_id)
    resets = sorted(
        (e['event_time'], e.get('received_at', 0), e['id'], _number(e['data'], 'prior_reading', 0.0))
        for e in events
        if e.get('kind') == 'meter_replacement' and e.get('data', {}).get('meter_id') == meter_id
    )
    seen = set()
    out = []
    prev = None
    reset_idx = 0
    for event in records:
        data = event['data']
        reading = _number(data, 'reading')
        key = (event['event_time'], reading)
        if key in seen:
            continue
        seen.add(key)
        while reset_idx < len(resets) and (resets[reset_idx][0], resets[reset_idx][1], resets[reset_idx][2]) <= (
                event['event_time'], event.get('received_at', 0), event['id']):
            # 换表归零：新表首读数以其自带 prior_reading 为前值
            prev = None
            reset_idx += 1
        if prev is None:
            baseline = _number(data, 'prior_reading', 0.0)
        else:
            baseline = prev
        raw = reading - baseline
        out.append((event['event_time'], raw, raw * _factor_at(factors, event['event_time']), event['id']))
        prev = reading
    return out


def meter_volume(events, meter_id):
    """校准后相邻差之和，即该表计测得的有向体积。"""
    return sum(item[2] for item in meter_deltas(events, meter_id))


def all_meter_volumes(events):
    ids = {e['data']['meter_id'] for e in events
           if e.get('kind') == 'meter_reading' and 'meter_id' in e.get('data', {})}
    return {meter_id: meter_volume(events, meter_id) for meter_id in sorted(ids)}


def unsigned_or_bad_readings(events):
    """验签失败的读数证据 ID（冻结节点时要给出对应仪表与读数）。"""
    return [e['id'] for e in events if e.get('kind') == 'meter_reading' and not verify_event(e)]


def grouped_readings(events):
    """按表计分组的原始读数，保留验签标记，供常规采集（即使全部验签通过也会算错120的场景）使用。"""
    groups = defaultdict(list)
    for event in sorted(events, key=lambda e: (e['event_time'], e.get('received_at', 0), e['id'])):
        if event.get('kind') != 'meter_reading':
            continue
        groups[event['data']['meter_id']].append(event)
    return dict(groups)
