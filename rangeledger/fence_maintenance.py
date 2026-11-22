from .fence_sensor import tripped_loops

def suspect_sections(readings, loops):
    return sorted({section for loop in tripped_loops(readings) for section in loops[loop]})
