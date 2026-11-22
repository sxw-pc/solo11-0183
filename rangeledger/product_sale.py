from .medicine import withdrawal_end

def may_ship(treatments, sale_day):
    return sale_day >= withdrawal_end(treatments)
