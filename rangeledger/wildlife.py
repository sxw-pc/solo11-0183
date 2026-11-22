from .camera_trap import visible_count

def sightings(frames):
    return sum(visible_count(frame) for frame in frames)
