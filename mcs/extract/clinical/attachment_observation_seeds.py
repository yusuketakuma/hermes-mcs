"""Seed OCR observations from shared source-verified laboratory and vital records."""
import re

import clinical_values
from clinical_scope import source_axes
from mcs_util import positive


_VITAL_NAMES = {
    "bt": ("体温", "BT", "Bt", "bt"),
    "hr": ("脈拍", "心拍", "HR", "Hr", "hr"),
    "rr": ("呼吸数", "RR", "Rr", "rr"),
    "spo2": ("酸素飽和度", "SpO2", "Spo2", "SPO2", "spo2"),
    "bs": ("血糖値", "血糖", "BS", "Bs", "bs"),
    "sbp": ("収縮期血圧", "収縮期"),
    "dbp": ("拡張期血圧", "拡張期"),
}


def seeds(document, source):
    """Keep explicit patient measurements and literal details without filling missing fields."""
    # Lazy import keeps the existing shared measurement validators' query imports out of module setup.
    from measurement_sources import _labs, _vitals

    if (not isinstance(document, dict) or source.get("source") != "attachment_ocr"
            or not positive(source.get("artifact_id"))):
        return []
    body = source.get("body_text")
    if not isinstance(body, str):
        return []
    message = {**source, "posted_at": None, "posted_at_ts": None}
    records, _ = _labs(message, document, source["artifact_id"], None, True)
    vitals, _ = _vitals(message, document, source["artifact_id"], True)
    result = []
    for record in (*records, *vitals):
        quote, unit = record["evidence"], record["unit"]
        if (record["verification"] != "quote_supported" or record["subject"] != "patient"
                or record["status"] not in ("current", "past") or not quote
                or not isinstance(unit, str) or not unit or unit not in quote):
            continue
        written = clinical_values.measurement_day(quote)
        dates = list(clinical_values._DATE.finditer(quote))
        if (written is None or written[0] != "ymd" or len(dates) != 1
                or record["measured_on"] != written[1].isoformat()):
            continue
        name = record["name"]
        if record["kind"] == "vital":
            name = next((name for name in _VITAL_NAMES.get(record["key"], ())
                         if quote.count(name) == 1), None)
        if not isinstance(name, str) or quote.count(name) != 1:
            continue
        anchor = body.find(name, *record["evidence_span"])
        if source_axes(body, anchor, anchor + len(name), default="unknown")[0] != "patient":
            continue
        value = str(record["value"])
        if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
            literal = re.search(r"(?<![\d.])" + re.escape(value) + r"(?![\d.])", quote)
            if literal is None:
                continue
            value = literal[0]
        elif not value or value not in quote:
            continue
        details = {"observation_name": name, "value": value, "unit": unit,
                   "measured_on": dates[0][0]}
        if record["condition"] is not None:
            details["condition"] = record["condition"]
        result.append({"category": "observations", "text": quote, "evidence": quote,
                       "subject": "patient", "details": [
                           {"key": key, "value": value, "evidence": quote}
                           for key, value in details.items()]})
    return result
