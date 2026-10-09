"""Blood pressure is one reading: the writer guard and the cached reader
never pair a systolic and a diastolic value from different measurements,
and a truncated 'BP' label is never read as a bare pulse 'P'. Fully
synthetic bodies; no model, ledger or network."""
import pytest

import clinical_values
import extract
import extract_llm

MORNING_EVENING = "本人の朝BP180/100、夕BP120/80"


def _layers(body, values):
    return (extract_llm._vitals_guard(body, dict(values)),
            extract.patient_vitals(dict(values), body),
            clinical_values.patient_current_vitals(dict(values), body))


@pytest.mark.parametrize("body", [
    MORNING_EVENING,
    "本人の朝BP180／100、夕BP120／80",                    # full-width slash
    "本人の朝BP 180 / 100、夕BP 120 / 80",                # spaced
    "本人の朝BP180/100、夕120/80",                        # unlabelled second reading
    "本人の朝 収縮期180 拡張期100、夕 収縮期120 拡張期80",  # side labels, no slash
    "本人の血圧 朝180/100\n夕120/80",                      # separate lines
])
def test_no_layer_pairs_values_from_different_readings(body):
    for layer in _layers(body, {"sbp": 180, "dbp": 80}):
        assert not ({"sbp", "dbp"} & layer.keys()), layer


@pytest.mark.parametrize("values", [{"sbp": 180, "dbp": 100}, {"sbp": 120, "dbp": 80}])
def test_a_reading_present_verbatim_is_kept(values):
    for layer in _layers(MORNING_EVENING, values):
        assert {k: layer[k] for k in ("sbp", "dbp")} == values


@pytest.mark.parametrize("values", [{"sbp": 180}, {"dbp": 80}])
def test_one_sided_value_keeps_its_literal_side(values):
    for layer in _layers(MORNING_EVENING, values):
        assert layer == values


def test_single_explicitly_labelled_reading_stays_compatible():
    body = "本人の血圧は収縮期180、拡張期100"
    for layer in _layers(body, {"sbp": 180, "dbp": 100}):
        assert layer == {"sbp": 180, "dbp": 100}


def test_family_pair_is_never_borrowed_for_the_patient():
    body = "母はBP180/80。本人は朝BP180/100、夕BP120/80"
    _, reader, current = _layers(body, {"sbp": 180, "dbp": 80})
    assert not ({"sbp", "dbp"} & reader.keys())
    assert not ({"sbp", "dbp"} & current.keys())


def test_other_vitals_survive_a_dropped_mixed_pair():
    body = "本人 BP180/100 脈80、夕BP120/70"
    for layer in _layers(body, {"sbp": 180, "dbp": 70, "hr": 80}):
        assert layer == {"hr": 80}


def test_dates_and_decimals_do_not_count_as_blood_pressure():
    body = "本人 10/12 BP180/100 体温36.5"
    for layer in _layers(body, {"sbp": 180, "dbp": 100, "bt": 36.5}):
        assert layer == {"sbp": 180, "dbp": 100, "bt": 36.5}


def test_repeated_identical_reading_is_one_reading():
    body = "本人の朝BP130/80、夕BP130/80"
    for layer in _layers(body, {"sbp": 130, "dbp": 80}):
        assert layer == {"sbp": 130, "dbp": 80}


def test_mixed_pair_is_noted_for_the_writer():
    drops = {}
    extract_llm._vitals_guard(MORNING_EVENING, {"sbp": 180, "dbp": 80}, drops)
    assert any("同じ測定" in note for note in drops["vitals"])


def test_truncated_bp_label_is_not_a_bare_pulse_p():
    body = "本人の朝BP180/100、夕120/80"
    start = body.rindex("80")
    assert extract_llm._nearest_vital_label(body, start, start + 2) != "hr"
    guarded = extract_llm._vitals_guard(body, {"sbp": 120, "dbp": 80})
    assert "hr" not in guarded and guarded == {"sbp": 120, "dbp": 80}


@pytest.mark.parametrize("body,value", [("BT36.5 P72", 72), ("体温36.5 P 72", 72)])
def test_a_real_bare_p_pulse_is_still_found(body, value):
    start = body.rindex(str(value))
    assert extract_llm._nearest_vital_label(body, start, start + len(str(value))) == "hr"


# ---------- evidence comes from the written label, never from the values ----------

@pytest.mark.parametrize("body,values", [
    ("本人の朝BP40/30、夕BP60/20", {"sbp": 40, "dbp": 20}),         # low readings
    ("本人の朝BP80/80、夕BP90/70", {"sbp": 80, "dbp": 70}),         # equal sides
    ("本人の朝BP70/90、夕BP80/60", {"sbp": 70, "dbp": 60}),         # reversed order
    ("本人の朝血圧180/100mmHg、夕血圧120/80mmHg", {"sbp": 180, "dbp": 80}),
])
def test_labelled_readings_count_whatever_their_values(body, values):
    for layer in _layers(body, values):
        assert not ({"sbp", "dbp"} & layer.keys()), layer


@pytest.mark.parametrize("body,values", [
    ("本人の朝BP40/30、夕BP60/20", {"sbp": 40, "dbp": 30}),
    ("本人のBP80/80", {"sbp": 80, "dbp": 80}),
    ("本人のBP70/90", {"sbp": 70, "dbp": 90}),
])
def test_labelled_low_equal_or_reversed_reading_is_kept(body, values):
    for layer in _layers(body, values):
        assert {k: layer[k] for k in ("sbp", "dbp")} == values


@pytest.mark.parametrize("body", [
    "10/12 本人のBP180/100",                       # a date before the label
    "本人のBP180/100 次回10/20訪問",               # a date after the reading
])
def test_unlabelled_dates_are_not_readings(body):
    for layer in _layers(body, {"sbp": 180, "dbp": 100}):
        assert {k: layer[k] for k in ("sbp", "dbp")} == {"sbp": 180, "dbp": 100}


# ---------- decimal readings follow the guard's own numeric grammar ----------

DECIMAL = "本人の朝BP180.5/100.5、夕BP120.5/80.5"


@pytest.mark.parametrize("body,values", [
    (DECIMAL, {"sbp": 180.5, "dbp": 80.5}),
    ("本人の朝BP180．5／100．5、夕BP120．5／80．5", {"sbp": 180.5, "dbp": 80.5}),
    ("母はBP180.5/80.5。本人は朝BP180.5/100.5、夕BP120.5/80.5", {"sbp": 180.5, "dbp": 80.5}),
    ("本人の朝 収縮期180.5 拡張期100.5、夕 収縮期120.5 拡張期80.5", {"sbp": 180.5, "dbp": 80.5}),
    ("本人の朝BP180.5/100、夕BP120/80.5", {"sbp": 180.5, "dbp": 80.5}),
])
def test_decimal_values_from_different_readings_are_not_paired(body, values):
    _, reader, current = _layers(body, values)
    assert not ({"sbp", "dbp"} & reader.keys()), reader
    assert not ({"sbp", "dbp"} & current.keys()), current
    if not body.startswith("母"):
        writer = extract_llm._vitals_guard(body, dict(values))
        assert not ({"sbp", "dbp"} & writer.keys()), writer


@pytest.mark.parametrize("values", [{"sbp": 180.5, "dbp": 100.5}, {"sbp": 120.5, "dbp": 80.5},
                                    {"sbp": 180.5}, {"dbp": 80.5}])
def test_decimal_reading_or_side_present_verbatim_is_kept(values):
    for layer in _layers(DECIMAL, values):
        assert {k: layer[k] for k in values} == values
