from rangeledger.wildlife import sightings

def test_two_frames_baseline():
    frames=[{'detections':['deer-1','deer-2']},{'detections':['deer-2','deer-3']}]
    assert sightings(frames)==4
