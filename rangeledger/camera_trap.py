"""红外相机帧基线：验签、按相机时间排序、相同标签去重。

visible_count 与基线去重只数“检测条数”，不等于鹿只总数；
跨相机个体级估算见 wildlife 的个体重识别图普查。
"""
import hashlib
import json


def visible_count(frame):
    return len(frame.get('detections', []))


def frame_signature(frame):
    """帧内容签名：相机、拍摄时刻、照片与检测列表的确定性摘要。"""
    core = {
        'camera': frame.get('camera'),
        'captured_at': frame.get('captured_at'),
        'photo': frame.get('photo'),
        'detections': frame.get('detections', []),
    }
    text = json.dumps(core, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


def verify_frame(frame):
    """验签并校验单帧结构；非法帧抛 ValueError。"""
    for key in ('id', 'camera', 'captured_at', 'photo', 'detections'):
        if key not in frame:
            raise ValueError('frame missing ' + key)
    detections = frame['detections']
    if not isinstance(detections, list) or not detections:
        raise ValueError('frame detections empty ' + frame['id'])
    labels = []
    for det in detections:
        if not isinstance(det, dict) or 'label' not in det:
            raise ValueError('detection missing label in frame ' + frame['id'])
        labels.append(det['label'])
    if len(set(labels)) != len(labels):
        raise ValueError('duplicate label in frame ' + frame['id'])
    sig = frame.get('signature')
    if sig is not None and sig != frame_signature(frame):
        raise ValueError('bad frame signature ' + frame['id'])
    return True


def sort_frames(frames):
    """按相机时间排序（拍摄时刻、相机、帧号）。"""
    return sorted(frames, key=lambda f: (f['captured_at'], f['camera'], f['id']))


def detection_refs(frame):
    """帧内检测的全局引用：帧号#局部标签。"""
    return [frame['id'] + '#' + det['label'] for det in frame.get('detections', [])]


def dedupe_same_labels(frames):
    """常规补丁的相同标签去重：跨帧同名字段只计第一次。

    两台相机的局部标签互异时结果仍是检测条数，不能当作鹿只总数。
    """
    seen = set()
    total = 0
    for frame in sort_frames(frames):
        for det in frame.get('detections', []):
            label = det.get('label') if isinstance(det, dict) else det
            if label in seen:
                continue
            seen.add(label)
            total += 1
    return total
