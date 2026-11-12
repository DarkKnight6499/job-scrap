from sponsorship_facets import facets


def test_refused_and_authorization():
    f = facets("Candidates must possess unrestricted work authorization and not require future sponsorship.")
    assert f["future_sponsorship"]["value"] == "refused" and f["current_authorization"]["value"] == "required"


def test_offered():
    f = facets("WSIB is open to candidates who may require employment-based visa sponsorship.")
    assert f["future_sponsorship"]["value"] == "offered" and "sponsorship" in f["future_sponsorship"]["quote"]


def test_clearance_and_silence():
    assert facets("Active Secret clearance required.")["citizenship"]["value"] == "clearance"
    assert all(v["value"] == "not stated" for v in facets("Treasury analyst, strong Excel.").values())
