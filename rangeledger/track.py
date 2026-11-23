import hashlib, hmac, json, math
from collections import defaultdict
from .common import rows, night_hours

MAX_GAP=2.0       # 相邻定位间隔超过该小时数视为断档，不得用最后坐标填满
MAX_SPEED=50.0    # 相邻定位隐含速度超过该值视为跳点
GATE_RADIUS=1.0   # 越界点落在门的该半径内则归属该门
DEFAULT_EPS=0.5   # 围栏边界误差带
BIG=10**12
KEYRING={'collar-7':'pasture-key-7','collar-9':'pasture-key-9'}  # 离线设备密钥登记簿

def _canonical(obj): return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'))

def _signed_body(data): return {k:data.get(k) for k in ('device','holder','movement','fixes')}

def sign_payload(data):
    return hmac.new(KEYRING[data['device']].encode(),_canonical(_signed_body(data)).encode(),hashlib.sha256).hexdigest()

def verify_package(data):
    if data.get('device') not in KEYRING: return '未知设备'
    fixes=data.get('fixes')
    if not fixes: return '轨迹为空'
    times=[f.get('t') for f in fixes]
    if any(not isinstance(t,(int,float)) for t in times): return '时间乱序'
    if any(b<=a for a,b in zip(times,times[1:])): return '时间乱序'
    if any(not isinstance(f.get('x'),(int,float)) or not isinstance(f.get('y'),(int,float)) for f in fixes): return '坐标缺失'
    if not hmac.compare_digest(sign_payload(data),str(data.get('signature',''))): return '签名不符'
    return None

def _digest(data): return hashlib.sha256(_canonical(_signed_body(data)).encode()).hexdigest()

def collect_packages(ev):
    accepted=[];duplicate=[];rejected=[];seen={}
    for e in sorted(rows(ev,'gps_package'),key=lambda e:(e['received_at'],e['event_time'],e['id'])):
        reason=verify_package(e['data'])
        if reason: rejected.append({'id':e['id'],'reason':reason}); continue
        digest=_digest(e['data'])
        if digest in seen: duplicate.append(e['id']); continue
        seen[digest]=e['id']; accepted.append(e)
    return accepted,duplicate,rejected

def latest_fences(ev):
    fences={}
    for e in sorted(rows(ev,'fence'),key=lambda e:(e['event_time'],e['received_at'],e['id'])):
        fences[e['data'].get('zone','Z1')]=e
    return fences

def permit_states(ev):
    groups=defaultdict(list)
    for e in rows(ev,'transit_permit'):
        groups[e['data'].get('permit_id',e['id'])].append(e)
    states={}
    for pid,events in groups.items():
        events.sort(key=lambda e:(e['event_time'],e['received_at'],e['id']))
        terms=next((x for x in reversed(events) if not x['data'].get('revoked')),events[-1])
        revoked=bool(events[-1]['data'].get('revoked'));d=terms['data']
        # 生效起点取有效期、签发时间与实际上传时间的最大者：迟到上传不得伪装成提前获批
        eff_from=max(float(d.get('valid_from',-BIG)),float(d.get('signed_at',-BIG)),float(terms['received_at']))
        eff_to=float(d.get('valid_to',BIG))
        if revoked: eff_to=min(eff_to,float(events[-1]['data'].get('revoked_at',eff_to)))
        states[pid]=dict(holder=d.get('holder','H1'),corridor=set(d.get('corridor',[])),eff_from=eff_from,eff_to=eff_to,revoked=revoked,versions=len(events))
    return states

def _inside(p,poly):
    x,y=p;inside=False
    for i in range(len(poly)):
        x1,y1=poly[i];x2,y2=poly[(i+1)%len(poly)]
        if (y1>y)!=(y2>y) and x<x1+(y-y1)*(x2-x1)/(y2-y1): inside=not inside
    return inside

def _dist_seg(p,a,b):
    px,py=p;ax,ay=a;dx,dy=b[0]-ax,b[1]-ay
    span=dx*dx+dy*dy
    if span==0: return math.hypot(px-ax,py-ay)
    u=max(0,min(1,((px-ax)*dx+(py-ay)*dy)/span))
    return math.hypot(px-ax-u*dx,py-ay-u*dy)

def _classify(p,poly,eps):
    if min(_dist_seg(p,poly[i],poly[(i+1)%len(poly)]) for i in range(len(poly)))<=eps: return 'boundary'
    return 'inside' if _inside(p,poly) else 'outside'

def _cross(p1,p2,a,b):
    den=(p2[0]-p1[0])*(b[1]-a[1])-(p2[1]-p1[1])*(b[0]-a[0])
    if abs(den)<1e-12: return None
    u=((a[0]-p1[0])*(b[1]-a[1])-(a[1]-p1[1])*(b[0]-a[0]))/den
    v=((a[0]-p1[0])*(p2[1]-p1[1])-(a[1]-p1[1])*(p2[0]-p1[0]))/den
    if 1e-9<u<1-1e-9 and -1e-9<=v<=1+1e-9: return u
    return None

def _position_at(pts,tau):
    for (t1,p1),(t2,p2) in zip(pts,pts[1:]):
        if t1-1e-9<=tau<=t2+1e-9:
            u=0 if t2==t1 else (tau-t1)/(t2-t1)
            return (p1[0]+u*(p2[0]-p1[0]),p1[1]+u*(p2[1]-p1[1]))
    return pts[0][1] if tau<pts[0][0] else pts[-1][1]

def _nearest_edge(p,poly):
    return min(range(len(poly)),key=lambda i:_dist_seg(p,poly[i],poly[(i+1)%len(poly)]))

def _gate_for(p,gates):
    best=None;dist=GATE_RADIUS
    for g in gates:
        d=math.hypot(p[0]-g['point'][0],p[1]-g['point'][1])
        if d<=dist: best=g['name'];dist=d
    return best

def segment_package(fixes,poly,eps,gates):
    pts=[(float(f['t']),(float(f['x']),float(f['y']))) for f in fixes]
    states=[_classify(p,poly,eps) for _,p in pts]
    raw=[]
    for i in range(len(pts)-1):
        (t1,p1),(t2,p2)=pts[i],pts[i+1];dt=t2-t1
        if dt>MAX_GAP: raw.append({'start':t1,'end':t2,'state':'unknown','reason':'断档'}); continue
        if math.hypot(p2[0]-p1[0],p2[1]-p1[1])/dt>MAX_SPEED: raw.append({'start':t1,'end':t2,'state':'unknown','reason':'跳点'}); continue
        cuts=sorted(u for ei in range(len(poly)) for u in [_cross(p1,p2,poly[ei],poly[(ei+1)%len(poly)])] if u is not None)
        for u0,u1 in zip([0.0]+cuts,cuts+[1.0]):
            if u1-u0<1e-12: continue
            mid=(p1[0]+(u0+u1)/2*(p2[0]-p1[0]),p1[1]+(u0+u1)/2*(p2[1]-p1[1]))
            st=_classify(mid,poly,eps)
            if st=='boundary': st=next((s for s in (states[i],states[i+1]) if s!='boundary'),'unknown')
            raw.append({'start':t1+dt*u0,'end':t1+dt*u1,'state':st,'reason':None})
    segs=[]
    for s in raw:
        if segs and segs[-1]['state']==s['state'] and segs[-1]['reason']==s['reason'] and abs(segs[-1]['end']-s['start'])<1e-9: segs[-1]['end']=s['end']
        else: segs.append(dict(s))
    crossings=[]
    for prev,cur in zip(segs,segs[1:]):
        pair=(prev['state'],cur['state'])
        if pair in (('inside','outside'),('outside','inside')):
            tau=cur['start'];pos=_position_at(pts,tau)
            crossings.append({'time':tau,'point':pos,'edge':_nearest_edge(pos,poly),'gate':_gate_for(pos,gates),'error':eps,'kind':'exit' if pair==('inside','outside') else 'entry'})
    return segs,crossings

def _merge_holder(groups):
    cuts=sorted({round(s[k],9) for _,segs in groups for s in segs for k in ('start','end')})
    merged=[]
    for a,b in zip(cuts,cuts[1:]):
        if b-a<1e-9: continue
        contrib={}
        for pid,segs in groups:
            seg=next((s for s in segs if s['start']-1e-9<=a and s['end']+1e-9>=b),None)
            if seg: contrib[pid]=(seg['state'],seg['reason'])
        if not contrib: continue
        states={c[0] for c in contrib.values()}
        if len(states)==1:
            state=states.pop();reason=next(iter(contrib.values()))[1]
        else:
            state,reason='unknown','包间冲突'
        merged.append({'start':a,'end':b,'state':state,'reason':reason,'evidence':sorted(contrib)})
    out=[]
    for s in merged:
        if out and out[-1]['state']==s['state'] and out[-1]['reason']==s['reason'] and out[-1]['evidence']==s['evidence'] and abs(out[-1]['end']-s['start'])<1e-9: out[-1]['end']=s['end']
        else: out.append(s)
    return out

def _clip_by_permits(seg,permits):
    uncovered=[(seg['start'],seg['end'])];covered=[]
    for pid in sorted(permits):
        p=permits[pid]
        if p['holder']!=seg['holder']: continue
        if seg.get('exit_gate') not in p['corridor'] or seg.get('entry_gate') not in p['corridor']: continue
        a,b=max(seg['start'],p['eff_from']),min(seg['end'],p['eff_to'])
        if a>=b-1e-9: continue
        rest=[]
        for u0,u1 in uncovered:
            if b<=u0+1e-9 or a>=u1-1e-9: rest.append((u0,u1)); continue
            if u0<a-1e-9: rest.append((u0,a))
            if b<u1-1e-9: rest.append((b,u1))
        uncovered=rest
        covered.append((a,b,pid))
    return uncovered,covered

def _round(x): return round(x+0.0,6)

def _crossing_row(c):
    return {'time':_round(c['time']),'gate':c['gate'],'edge':c['edge'],'error':c['error']} if c else None

def analyze(ev):
    if not rows(ev,'gps_package') and not rows(ev,'fence'): return None
    accepted,duplicate,rejected=collect_packages(ev)
    fences=latest_fences(ev);permits=permit_states(ev)
    moves={m['id']:m for m in rows(ev,'movement')}
    by_holder=defaultdict(list);crossings=defaultdict(list);tracked=set();used=set()
    for e in accepted:
        d=e['data'];holder=d.get('holder','H1')
        if d.get('movement'): tracked.add(d['movement'])
        zone=d.get('zone') or (moves[d['movement']]['data'].get('zone') if d.get('movement') in moves else None) or (next(iter(fences)) if len(fences)==1 else None)
        fence=fences.get(zone) if zone else None
        if not fence:
            fixes=d['fixes'];by_holder[holder].append((e['id'],[{'start':float(fixes[0]['t']),'end':float(fixes[-1]['t']),'state':'unknown','reason':'缺少地籍'}])); continue
        used.add(fence['id']);fd=fence['data']
        segs,cross=segment_package(d['fixes'],[tuple(p) for p in fd['polygon']],float(fd.get('eps',DEFAULT_EPS)),fd.get('gates',[]))
        by_holder[holder].append((e['id'],segs));crossings[holder].extend(cross)
    table=[];night_by_holder=defaultdict(float)
    for holder in sorted(by_holder):
        for seg in _merge_holder(by_holder[holder]):
            if seg['state']=='inside': continue
            base={'holder':holder,'evidence':sorted(set(seg['evidence'])|used)}
            if seg['state']=='unknown':
                table.append(dict(base,start=_round(seg['start']),end=_round(seg['end']),hours=_round(seg['end']-seg['start']),night=_round(night_hours(seg['start'],seg['end'])),state='待核',reason=seg['reason'],exit=None,entry=None,permit=None))
                continue
            seg['holder']=holder
            exit_x=next((c for c in crossings[holder] if c['kind']=='exit' and abs(c['time']-seg['start'])<1e-6),None)
            entry_x=next((c for c in crossings[holder] if c['kind']=='entry' and abs(c['time']-seg['end'])<1e-6),None)
            seg['exit_gate']=exit_x['gate'] if exit_x else None
            seg['entry_gate']=entry_x['gate'] if entry_x else None
            base.update(exit=_crossing_row(exit_x),entry=_crossing_row(entry_x))
            uncovered,covered=_clip_by_permits(seg,permits)
            for a,b,pid in covered:
                table.append(dict(base,start=_round(a),end=_round(b),hours=_round(b-a),night=_round(night_hours(a,b)),state='许可覆盖',reason=None,permit=pid))
            for a,b in uncovered:
                table.append(dict(base,start=_round(a),end=_round(b),hours=_round(b-a),night=_round(night_hours(a,b)),state='越界',reason=None,permit=None))
                night_by_holder[holder]+=night_hours(a,b)
    table.sort(key=lambda r:(r['holder'],r['start'],r['state']))
    overlay={pid:{'holder':p['holder'],'effective_from':_round(p['eff_from']),'effective_to':_round(p['eff_to']),'revoked':p['revoked'],'versions':p['versions'],'covered':[[r['start'],r['end']] for r in table if r.get('permit')==pid]} for pid,p in sorted(permits.items())}
    evidence_ids=sorted({e['id'] for e in accepted}|set(duplicate)|{r['id'] for r in rejected}|used|{e['id'] for e in rows(ev,'transit_permit')})
    ingestion={'accepted':sorted(e['id'] for e in accepted),'duplicate':sorted(duplicate),'rejected':sorted(rejected,key=lambda r:r['id']),'cadastre':{z:fences[z]['id'] for z in sorted(fences)}}
    return {'ingestion':ingestion,'table':table,'permits':overlay,'night_by_holder':dict(night_by_holder),'night_total':_round(sum(night_by_holder.values())),'tracked':tracked,'evidence':evidence_ids}
