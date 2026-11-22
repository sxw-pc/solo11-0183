from .water_meter import delta

def used(readings):
    return sum(delta(a,b) for a,b in zip(readings,readings[1:]))
