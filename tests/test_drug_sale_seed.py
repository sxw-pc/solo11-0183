from rangeledger.product_sale import may_ship

def test_one_treatment_baseline():
    assert may_ship([{'day': 3, 'withdraw_days': 7}], 11)
