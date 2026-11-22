from rangeledger.fence_maintenance import suspect_sections

def test_two_tripped_loops_baseline():
    readings = [{'loop': 'AB', 'alarm': True}, {'loop': 'BC', 'alarm': True}]
    assert suspect_sections(readings, {'AB': ['A', 'B'], 'BC': ['B', 'C']}) == ['A', 'B', 'C']
