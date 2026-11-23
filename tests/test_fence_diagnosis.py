"""围栏报警诊断、隔离复测与维修工单验收测试。

覆盖：
1. 基线调用链 fence_sensor.tripped_loops -> fence_maintenance.suspect_sections；
2. 两阶段报警下最小一致故障集的变化（互斥候选全部保留）；
3. 报警解释边、并集语义与“常规补丁必败”的反模式断言；
4. 已签名/缺签名/被篡改隔离复测下两种授权规程的不同工单；
5. 旧版拓扑勘误后的决定版次变化，以及重放幂等。
"""

import copy
import json
from pathlib import Path

import pytest

from rangeledger.fence_maintenance import (
    RETEST_SECRET,
    RetestSignatureError,
    alarm_explanation_edges,
    decide,
    location_procedure,
    minimal_fault_sets,
    night_safety_procedure,
    retest_proven_breaks,
    sign_retest,
    suspect_sections,
    validate_topology,
    verify_retest,
)
from rangeledger.fence_sensor import failed_self_checks, tripped_loops
from rangeledger.store import connect, evidence, ingest, report, save_decisions

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "fence-loops.json"

TOPO = {"AB": ["A", "B"], "BC": ["B", "C"], "AC": ["A", "C"]}
TOPO_OLD = {"AB": ["A", "B"], "BC": ["B", "C"], "AC": ["A", "C", "X"]}

READINGS_PHASE1 = [
    {"loop": "AB", "alarm": True, "self_check": True},
    {"loop": "BC", "alarm": True, "self_check": True},
    {"loop": "AC", "alarm": False, "self_check": True},
]
READINGS_PHASE2 = READINGS_PHASE1[:2] + [
    {"loop": "AC", "alarm": True, "self_check": True},
]


def _event(event_id, kind, time, data):
    return {
        "id": event_id,
        "kind": kind,
        "subject": "fence-north",
        "event_time": time,
        "received_at": time,
        "data": data,
    }


def _topo_event(rev, loops, time):
    return _event(f"topo-v{rev}", "fence_topology", time,
                  {"revision": rev, "loops": loops})


def _alarm_event(event_id, time, readings):
    return _event(event_id, "fence_alarm", time, {"readings": readings})


def _retest_event(time, data, event_id="retest-c"):
    return _event(event_id, "fence_retest", time, data)


SIGNED_RETEST = {
    "retest_id": "retest-c-0001",
    "isolated": "C",
    "branches": [
        {"sections": ["A"], "alarm": True},
        {"sections": ["B"], "alarm": True},
    ],
}


@pytest.fixture
def signed_retest():
    data = copy.deepcopy(SIGNED_RETEST)
    data["signature"] = sign_retest(data)
    return data


# ---------------------------------------------------------------- 基线调用链

def test_sensor_to_maintenance_baseline_union():
    # tests/test_fence_seed.py 的固定契约：AB、BC 报警 -> A/B/C
    assert tripped_loops(READINGS_PHASE1) == ["AB", "BC"]
    assert suspect_sections(READINGS_PHASE1, TOPO) == ["A", "B", "C"]


def test_tripped_loops_dedupes_sorts_and_skips_failed_self_check():
    rows = [
        {"loop": "BC", "alarm": True},
        {"loop": "AB", "alarm": True},
        {"loop": "BC", "alarm": True},
        {"loop": "AC", "alarm": True, "self_check": False},
    ]
    assert tripped_loops(rows) == ["AB", "BC"]
    assert failed_self_checks(rows) == ["AC"]


def test_sensor_requires_loop_and_alarm_fields():
    with pytest.raises(ValueError):
        tripped_loops([{"loop": "AB"}])


# ------------------------------------------------------------ 最小一致故障集

def test_phase1_two_alarms_have_two_minimal_solutions():
    tripped = tripped_loops(READINGS_PHASE1)
    candidates = [sorted(c) for c in minimal_fault_sets(tripped, TOPO)]
    assert candidates == [["B"], ["A", "C"]]
    # 并集仍是 A/B/C，但并集本身不是最小解
    assert suspect_sections(READINGS_PHASE1, TOPO) == ["A", "B", "C"]
    assert ["A", "B", "C"] not in candidates


def test_phase2_three_alarms_have_three_pairwise_solutions():
    tripped = tripped_loops(READINGS_PHASE2)
    candidates = [sorted(c) for c in minimal_fault_sets(tripped, TOPO)]
    assert candidates == [["A", "B"], ["A", "C"], ["B", "C"]]
    # 源码并集仍为 A/B/C，新诊断却全部是两段最小集
    assert suspect_sections(READINGS_PHASE2, TOPO) == ["A", "B", "C"]
    assert all(len(c) == 2 for c in candidates)


def test_conventional_patches_cannot_claim_a_confirmed_fault():
    tripped = tripped_loops(READINGS_PHASE2)
    loops_as_sets = [set(TOPO[loop]) for loop in tripped]
    # 交集补丁：三回路交集为空，解释不了任何报警
    assert set.intersection(*loops_as_sets) == set()
    candidates = minimal_fault_sets(tripped, TOPO)
    # 并集补丁：A/B/C 非最小
    assert {"A", "B", "C"} not in candidates
    # 频次/单点补丁：没有任何单段能同时解释三条报警
    assert all(len(c) >= 2 for c in candidates)
    # A/B 与实际 A/C、B/C 产生完全相同的三报警，无法区分，必须并列保留
    assert {"A", "B"} in [set(c) for c in candidates]
    assert {"A", "C"} in [set(c) for c in candidates]
    assert {"B", "C"} in [set(c) for c in candidates]
    # 逐回路各选一段可能拼出三段；枚举结果杜绝三段候选
    assert max(len(c) for c in candidates) == 2


def test_every_candidate_explains_every_alarm_and_is_minimal():
    tripped = tripped_loops(READINGS_PHASE2)
    for candidate in minimal_fault_sets(tripped, TOPO):
        for loop in tripped:
            assert candidate & set(TOPO[loop])  # 每条报警回路都被击中
        for section in candidate:
            reduced = candidate - {section}
            assert any(not (reduced & set(TOPO[loop])) for loop in tripped)


def test_no_alarm_has_no_fault_to_explain():
    assert minimal_fault_sets([], TOPO) == [set()]


def test_topology_validation_rejects_unknown_loop_and_bad_shape():
    with pytest.raises(ValueError):
        minimal_fault_sets(["ZZ"], TOPO)
    with pytest.raises(ValueError):
        validate_topology({"AB": []})
    with pytest.raises(ValueError):
        validate_topology({"AB": ["A", "A"]})


# -------------------------------------------------------------- 报警解释边

def test_alarm_explanation_edges_phase1():
    edges = alarm_explanation_edges(tripped_loops(READINGS_PHASE1), TOPO)
    assert edges == [("AB", "A"), ("AB", "B"), ("BC", "B"), ("BC", "C")]


def test_alarm_explanation_edges_phase2_adds_ac_branches():
    edges = alarm_explanation_edges(tripped_loops(READINGS_PHASE2), TOPO)
    assert edges == [
        ("AB", "A"), ("AB", "B"),
        ("AC", "A"), ("AC", "C"),
        ("BC", "B"), ("BC", "C"),
    ]


# -------------------------------------------------------------- 隔离复测验签

def test_signed_retest_verifies_and_proves_a_b_broken(signed_retest):
    verify_retest(signed_retest)
    assert retest_proven_breaks(signed_retest, TOPO) == {"A", "B"}


def test_missing_signature_forbids_location(signed_retest):
    unsigned = {k: v for k, v in signed_retest.items() if k != "signature"}
    with pytest.raises(RetestSignatureError, match="缺少授权签名"):
        verify_retest(unsigned)
    candidates = minimal_fault_sets(tripped_loops(READINGS_PHASE2), TOPO)
    with pytest.raises(RetestSignatureError):
        location_procedure(candidates, unsigned, TOPO, ["x"])


def test_tampered_retest_fails_verification(signed_retest):
    tampered = copy.deepcopy(signed_retest)
    tampered["isolated"] = "A"
    with pytest.raises(RetestSignatureError, match="签名不符"):
        verify_retest(tampered)


def test_retest_branch_with_unknown_section_rejected():
    # 结构校验独立于验签：即使绕过签名层，未知区段也必须被解释阶段拒绝。
    unsigned = copy.deepcopy(SIGNED_RETEST)
    unsigned["branches"] = [{"sections": ["Z"], "alarm": True}]
    with pytest.raises(RetestSignatureError):
        retest_proven_breaks(unsigned, TOPO)


def test_retest_branch_cannot_contain_isolated_section():
    # 正常授权链路下追加/篡改支路会因载荷改变而验签失败，定位被禁止；
    # 绕过验签直接解释时，含已切离区段的支路同样被结构校验拦截。
    tampered = copy.deepcopy(SIGNED_RETEST)
    tampered["branches"].append({"sections": ["C"], "alarm": True})
    with pytest.raises(RetestSignatureError):
        verify_retest(tampered)
    unsigned = copy.deepcopy(SIGNED_RETEST)
    unsigned["branches"].append({"sections": ["C"], "alarm": True})
    with pytest.raises(RetestSignatureError):
        retest_proven_breaks(unsigned, TOPO)


def test_wrong_secret_rejected():
    data = copy.deepcopy(SIGNED_RETEST)
    data["signature"] = sign_retest(data, secret=b"attacker-secret")
    with pytest.raises(RetestSignatureError):
        verify_retest(data)


# -------------------------------------------------------- 两种规程与两种工单

def test_night_safety_orders_all_three_sections_keeps_candidates():
    candidates = minimal_fault_sets(tripped_loops(READINGS_PHASE2), TOPO)
    result = night_safety_procedure(candidates, ["alarm-1", "alarm-2"])
    assert result["status"] == "执行"
    assert [o["section"] for o in result["work_orders"]] == ["A", "B", "C"]
    assert all(o["kind"] == "预防检查工单" for o in result["work_orders"])
    # 夜间规程不得收敛候选：三组互斥解原样保留
    assert result["candidates"] == [["A", "B"], ["A", "C"], ["B", "C"]]


def test_location_orders_only_proven_a_b(signed_retest):
    candidates = minimal_fault_sets(tripped_loops(READINGS_PHASE2), TOPO)
    result = location_procedure(candidates, signed_retest, TOPO,
                                ["alarm-1", "alarm-2", "retest-1"])
    assert result["status"] == "定位执行"
    assert result["proven_broken"] == ["A", "B"]
    assert result["candidates"] == [["A", "B"]]
    assert [o["section"] for o in result["work_orders"]] == ["A", "B"]
    assert all(o["kind"] == "定位维修工单" for o in result["work_orders"])
    assert result["evidence_ids"] == ["alarm-1", "alarm-2", "retest-1"]


def test_two_procedures_read_same_input_but_diverge(signed_retest):
    candidates = minimal_fault_sets(tripped_loops(READINGS_PHASE2), TOPO)
    night = night_safety_procedure(candidates, ["alarm-1"])
    located = location_procedure(candidates, signed_retest, TOPO,
                                 ["alarm-1", "retest-1"])
    night_sections = {o["section"] for o in night["work_orders"]}
    locate_sections = {o["section"] for o in located["work_orders"]}
    assert night_sections == {"A", "B", "C"}
    assert locate_sections == {"A", "B"}
    assert len(night["candidates"]) == 3 and len(located["candidates"]) == 1


# ------------------------------------------------------- decide 两阶段与版次

def _open_db(tmp_path):
    return connect(tmp_path / "fence.db")


def _recompute(db, subject="fence-north"):
    with db:
        results = decide(evidence(db, subject))
        save_decisions(db, subject, results)


def test_decide_two_phase_candidate_changes_and_work_orders(tmp_path, signed_retest):
    db = _open_db(tmp_path)
    with db:
        ingest(db, _topo_event(2, TOPO, 20))
        ingest(db, _alarm_event("alarm-p1", 30, READINGS_PHASE1))
    _recompute(db)
    phase1 = report(db, "fence-north")
    diag1 = phase1["围栏最小故障集诊断"]["result"]
    assert diag1["tripped_loops"] == ["AB", "BC"]
    assert diag1["candidates"] == [["B"], ["A", "C"]]
    assert phase1["夜间安全预防工单"]["result"]["work_orders"] and \
        len(phase1["夜间安全预防工单"]["result"]["work_orders"]) == 3
    # 阶段一没有复测：定位规程被禁止，候选原样保留
    blocked = phase1["隔离定位维修工单"]["result"]
    assert blocked["status"] == "禁止"
    assert blocked["candidates"] == [["B"], ["A", "C"]]
    assert blocked["work_orders"] == []

    # 阶段二：AC 加入报警，候选变为三组两段解
    with db:
        ingest(db, _alarm_event("alarm-p2", 40, READINGS_PHASE2))
    _recompute(db)
    phase2 = report(db, "fence-north")
    assert phase2["围栏最小故障集诊断"]["result"]["candidates"] == \
        [["A", "B"], ["A", "C"], ["B", "C"]]
    assert all(v["version"] == 2 for v in phase2.values())
    # 复测入库：定位规程放行，只下 A/B 工单，候选收敛
    with db:
        ingest(db, _retest_event(50, signed_retest))
    _recompute(db)
    phase3 = report(db, "fence-north")
    location = phase3["隔离定位维修工单"]["result"]
    assert location["status"] == "定位执行"
    assert location["proven_broken"] == ["A", "B"]
    assert [o["section"] for o in location["work_orders"]] == ["A", "B"]
    assert location["candidates"] == [["A", "B"]]
    # 夜间规程不受复测影响：仍是 A/B/C 预防工单与三组候选
    night = phase3["夜间安全预防工单"]["result"]
    assert [o["section"] for o in night["work_orders"]] == ["A", "B", "C"]
    assert night["candidates"] == [["A", "B"], ["A", "C"], ["B", "C"]]
    # 只有定位决定在第三阶段再次升版
    assert phase3["隔离定位维修工单"]["version"] == 3
    assert phase3["围栏最小故障集诊断"]["version"] == 2
    db.close()


def test_decide_unsigned_retest_keeps_location_forbidden(tmp_path):
    db = _open_db(tmp_path)
    unsigned = {k: v for k, v in SIGNED_RETEST.items()}  # 无 signature
    with db:
        ingest(db, _topo_event(2, TOPO, 20))
        ingest(db, _alarm_event("alarm-p2", 40, READINGS_PHASE2))
        ingest(db, _retest_event(50, unsigned))
    _recompute(db)
    result = report(db, "fence-north")
    location = result["隔离定位维修工单"]["result"]
    assert location["status"] == "禁止"
    assert "签名" in location["reason"]
    assert location["work_orders"] == []
    # 夜间规程照常，且复测证据 ID 仍被记录，审计可追溯
    assert result["夜间安全预防工单"]["result"]["status"] == "执行"
    assert "retest-c" in location["evidence_ids"]
    db.close()


def test_decide_self_check_failure_rejects_untrusted_alarm(tmp_path):
    db = _open_db(tmp_path)
    bad_readings = [
        {"loop": "AB", "alarm": True, "self_check": False},
        {"loop": "BC", "alarm": True, "self_check": True},
    ]
    with db:
        ingest(db, _topo_event(2, TOPO, 20))
        ingest(db, _alarm_event("alarm-bad", 40, bad_readings))
    with pytest.raises(ValueError, match="自检失败"):
        decide(evidence(db, "fence-north"))
    db.close()


# --------------------------------------------------- 拓扑勘误旧版与幂等持久化

def test_topology_correction_removes_spurious_x_candidate(tmp_path):
    db = _open_db(tmp_path)
    with db:
        ingest(db, _topo_event(1, TOPO_OLD, 10))
        ingest(db, _alarm_event("alarm-p2", 40, READINGS_PHASE2))
    _recompute(db)
    old = report(db, "fence-north")
    old_candidates = old["围栏最小故障集诊断"]["result"]["candidates"]
    # 旧拓扑 AC 误挂 X：产生含 X 的伪最小集 {B, X}
    assert ["B", "X"] in old_candidates
    assert any("X" in s for s in old_candidates)

    # 新版拓扑勘误入库后，诊断升版且 X 消失
    with db:
        ingest(db, _topo_event(2, TOPO, 20))
    _recompute(db)
    new = report(db, "fence-north")
    new_candidates = new["围栏最小故障集诊断"]["result"]["candidates"]
    assert new_candidates == [["A", "B"], ["A", "C"], ["B", "C"]]
    assert all("X" not in s for s in new_candidates)
    assert new["围栏最小故障集诊断"]["version"] == 2
    # 旧版证据仍在库中可追溯，最新报告只引用新版拓扑
    assert "topo-v1" in [e["id"] for e in evidence(db, "fence-north")]
    assert "topo-v2" in new["围栏最小故障集诊断"]["evidence_ids"]
    assert "topo-v1" not in new["围栏最小故障集诊断"]["evidence_ids"]
    db.close()


def test_recompute_is_idempotent_and_conflicting_identity_rejected(tmp_path):
    db = _open_db(tmp_path)
    events = [
        _topo_event(2, TOPO, 20),
        _alarm_event("alarm-p1", 30, READINGS_PHASE1),
        _alarm_event("alarm-p2", 40, READINGS_PHASE2),
    ]
    with db:
        for event in events:
            ingest(db, event)
    _recompute(db)
    before = report(db, "fence-north")
    # 重复摄入同证据不产生新版本
    with db:
        for event in events:
            assert ingest(db, event) is False
    _recompute(db)
    assert report(db, "fence-north") == before
    # 同 ID 冲突证据被拒绝
    conflict = _alarm_event("alarm-p1", 30, READINGS_PHASE2)
    with pytest.raises(ValueError, match="conflicting evidence"):
        ingest(db, conflict)
    assert report(db, "fence-north") == before
    db.close()


# ------------------------------------------------------------- 交付 fixture

def test_fixture_is_self_consistent_and_signed():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    events = fixture["events"]
    kinds = [e["kind"] for e in events]
    assert kinds.count("fence_topology") == 2
    assert kinds.count("fence_alarm") == 2
    assert kinds.count("fence_retest") == 1
    retest = next(e for e in events if e["kind"] == "fence_retest")["data"]
    verify_retest(retest)  # fixture 内的签名必须可直接验签
    assert retest_proven_breaks(retest, TOPO) == {"A", "B"}
    # 旧版拓扑确含 X，新版勘误
    topos = {e["data"]["revision"]: e["data"]["loops"]
             for e in events if e["kind"] == "fence_topology"}
    assert "X" in topos[1]["AC"] and topos[2]["AC"] == ["A", "C"]


def test_fixture_end_to_end_through_store(tmp_path):
    db = _open_db(tmp_path)
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    with db:
        for event in fixture["events"]:
            ingest(db, event)
    _recompute(db)
    result = report(db, "fence-north")
    assert result["围栏最小故障集诊断"]["result"]["candidates"] == \
        [["A", "B"], ["A", "C"], ["B", "C"]]
    edges = {(e["loop"], e["section"])
             for e in result["围栏报警解释边"]["result"]}
    assert edges == {
        ("AB", "A"), ("AB", "B"), ("AC", "A"), ("AC", "C"),
        ("BC", "B"), ("BC", "C"),
    }
    night = result["夜间安全预防工单"]["result"]
    locate = result["隔离定位维修工单"]["result"]
    assert [o["section"] for o in night["work_orders"]] == ["A", "B", "C"]
    assert locate["status"] == "定位执行"
    assert [o["section"] for o in locate["work_orders"]] == ["A", "B"]
    # 再跑一遍完全幂等
    versions_before = {k: v["version"] for k, v in result.items()}
    _recompute(db)
    versions_after = {k: v["version"]
                      for k, v in report(db, "fence-north").items()}
    assert versions_before == versions_after
    db.close()


def test_retest_secret_is_constant_across_module():
    # 签名机制必须稳定：fixture 中的签名由模块常量验证
    assert isinstance(RETEST_SECRET, bytes) and RETEST_SECRET
