from rangeledger.public_report import release

def test_three_holder_baseline():
    rows=[{'holder':k,'animal_hours':v} for k,v in [('A',10),('B',20),('C',30)]]
    assert release(rows)=={'holders':3,'animal_hours':60}
