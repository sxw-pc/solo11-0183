from rangeledger.water_ledger import used

def test_monotone_meter_baseline():
    assert used([100,105,110])==10
