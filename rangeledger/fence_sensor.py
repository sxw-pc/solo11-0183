def tripped_loops(readings):
    return [row['loop'] for row in readings if row['alarm']]
