"""Format grounded clinical values without database or transport operations."""

import clinical_values


def item_scope(item, context, surface=None):
    body, name, default = context
    if body is None:
        return default  # retained parsed-only callers have no original body
    return clinical_values.patient_item_scope(item, body, patient_name=name, surface=surface, default=default)


def scoped_vitals(values, context):

    body, name, default = context
    if body is None:
        return values
    values = clinical_values.patient_vitals(values, body, patient_name=name)
    if default != "patient":
        values = {key: value for key, value in values.items()
                  if item_scope({}, context, str(value)) == "patient"}
    return values


def vital_line(llm: dict | None, v1: dict, body=None, *, patient_name=None, default="patient"):
    selected = llm is not None
    llm = llm or {}
    lv = llm.get("vitals") if isinstance(llm.get("vitals"), dict) else {}
    vv = v1.get("vitals") if isinstance(v1.get("vitals"), dict) else {}
    # A missing LLM key may be an intentional subject/time exclusion.
    # Keep a reading intact; never construct a BP pair across sources.
    vit = lv if selected else vv
    vit = scoped_vitals(vit, (body, patient_name, default))
    if not vit:
        return None
    parts = []
    if vit.get("sbp") is not None or vit.get("dbp") is not None:
        # a half-extracted pair renders its known side only — never
        # the literal "None" in a clinical line
        sbp, dbp = vit.get("sbp"), vit.get("dbp")
        parts.append(f"BP {'?' if sbp is None else sbp}"
                     f"/{'?' if dbp is None else dbp}")
    for k, lab in (("bt", "BT"), ("hr", "HR"), ("rr", "RR"),
                   ("spo2", "SpO2"), ("bs", "BS")):
        if vit.get(k) is not None:
            parts.append(f"{lab} {vit[k]}")
    return "バイタル: " + "  ".join(parts) if parts else None
