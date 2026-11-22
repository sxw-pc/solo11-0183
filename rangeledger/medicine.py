def withdrawal_end(treatments):
    return max((row['day'] + row['withdraw_days'] for row in treatments), default=0)
