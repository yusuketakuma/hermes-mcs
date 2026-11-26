"""Unitless medication amounts must remain visible as unverified lab candidates."""
import extract_validation


def test_prescription_lines_keep_unitless_electrolyte_amounts_unverified():
    items = (("カリウム", ("4", "5"), "塩化"), ("カルシウム", ("8", "9"), "乳酸"),
             ("ナトリウム", ("120", "150"), "炭酸水素"))
    instructions = ("分2", "分３で継続", "朝夕食後", "朝1錠", "1日2回", "1回1包",
                    "就寝前に服用", "頓服", "シロップ1mL", "カプセル1個")
    for name, values, stem in items:
        for value in values:
            for label in (name, stem + name, f"{stem} {name}", f"処方：{name}"):
                for separator in (" ", "、", "，"):
                    for instruction in instructions:
                        body = f"{label} {value}{separator}{instruction}"
                        raw = {"name": name, "value": value, "evidence": body}
                        lab, = extract_validation.validate({"labs": [raw]}, body)["labs"]
                        assert lab["normalized"]["confirmation"] == "unverified", body
                        assert lab["value"] == value and lab["unverified"], body


def test_electrolyte_results_keep_values_across_layouts_and_ranges():
    items = (("カリウム", "mEq/L", ("3.7", "6.8"), ("1.2", "7.5")),
             ("ナトリウム", "mEq/L", ("134", "162"), ("111", "170")),
             ("カルシウム", "mg/dL", ("8.3", "11.8"), ("5.3", "13.7")))
    for name, unit, inside, outside in items:
        for value in (*inside, *outside):
            reading = f"{name} {value}"
            for quote, expected in (
                    (reading, value in inside), (f"採血 {reading}", value in inside),
                    (f"採血\n{reading}\nCr 0.8", value in inside),
                    (f"{reading}、Cr 0.8", value in inside),
                    (f"採血 {reading}{unit}", True), (f"食後 {reading}", False)):
                raw = {"name": name, "value": value, "evidence": quote}
                lab, = extract_validation.validate({"labs": [raw]}, quote)["labs"]
                assert lab["value"] == value, quote
                assert (lab["normalized"]["confirmation"] == "quote_supported") == expected, quote
