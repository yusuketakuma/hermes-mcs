"""Project literal PCA candidates with same-line written measurement dates, never dosing advice."""
import re

import medical_document
from referral_document import written_dates

SCHEMA = 'pump-document/v1'
_MAX_RECORDS = 4096
_MEASUREMENT_DAY = re.compile(r'(?:測定日時|測定日|観察日時|観察日|計測日時|計測日)[：: \t]')
_PUMP = re.compile(r'PCA|ポンプ', re.I)


def project(pages):
    """Keep PCA text and optional quantities unverified, with no date or dose imputation."""
    candidates = (medical_document.extract(pages)['pump_candidates']
                  if any(_PUMP.search(page['text']) for page in pages) else [])
    output = {'schema': SCHEMA, 'identified': bool(candidates), 'events': []}
    records = 0
    for candidate in candidates:
        evidence = candidate['evidence']
        cue = _MEASUREMENT_DAY.search(evidence)
        dates = written_dates(evidence[cue.end():]) if cue else []
        if dates and not evidence[cue.end():].lstrip().startswith(dates[0]['raw']):
            dates = []
        if records + 1 + len(dates) > _MAX_RECORDS:
            output['limited'] = True
            break
        records += 1 + len(dates)
        distinct = {(date['value'], date['precision'], date['raw'] if not date['valid'] else None)
                    for date in dates}
        state = ('ambiguous' if len(distinct) > 1 or any(not date['valid'] for date in dates)
                 else 'dated' if dates else 'undated')
        output['events'].append({**candidate, 'text': evidence, 'field_label': candidate['label'],
            'candidate_status': candidate['status'], 'status': 'unverified', 'unverified': True,
            'dates': dates, 'time': dates[0] if state == 'dated' else None, 'time_state': state})
    return output
