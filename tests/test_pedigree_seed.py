from rangeledger.breeding import shared_parent_count

def test_shared_parent_baseline():
    animals = {'S': {}, 'D1': {}, 'D2': {}, 'A': {'sire': 'S', 'dam': 'D1'}, 'B': {'sire': 'S', 'dam': 'D2'}}
    assert shared_parent_count(animals, 'A', 'B') == 1
