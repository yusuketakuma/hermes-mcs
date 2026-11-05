"""PCA projections preserve candidate status and never infer a date or dose."""
import pump_document


def _project(text):
    return pump_document.project([{'page': 1, 'text': text}])


def test_pump_candidate_keeps_number_literal_and_written_measurement_day():
    text = 'PCAポンプ\n測定日:2026年11月5日 PCA残量:10mL\n'
    row, = _project(text)['events']
    assert row['number'] == '10' and row['unit'] == 'mL'
    assert row['status'] == 'unverified' and row['unverified'] is True
    assert row['time']['value'] == '2026-11-05'
    assert row['text'] == text[slice(*row['evidence_span'])]


def test_document_date_does_not_date_undated_or_separate_line_candidate():
    rows = _project('PCAポンプ\n作成日:2026年11月5日\n測定日:2026年11月4日\n残量:10mL\n')['events']
    assert rows[0]['dates'] == [] and rows[0]['time'] is None
    assert not _project('食事残量:10mL')['identified']


def test_multiple_dates_and_missing_year_stay_unresolved():
    multiple, = _project('PCAポンプ\n測定日:2026年11月4日〜2026年11月5日 残量:10mL\n')['events']
    short, = _project('PCAポンプ\n測定日:11月5日 残量:10mL\n')['events']
    assert multiple['time_state'] == 'ambiguous' and multiple['time'] is None
    assert short['time']['value'] == '--11-05' and short['time']['precision'] == 'month_day'


def test_ambiguous_quantity_and_denial_are_not_confirmed():
    row, = _project('PCAポンプ\n測定日:2026年11月5日 残量:10mL、これは誤記で未確認\n')['events']
    assert row['candidate_status'] == 'needs_review'
    assert 'number' not in row and row['unverified'] is True
    assert '誤記で未確認' in row['evidence']


def test_candidate_limit_is_explicit_and_counts_date_candidates(monkeypatch):
    monkeypatch.setattr(pump_document, '_MAX_RECORDS', 1)
    result = _project('PCAポンプ\n測定日:2026年11月5日 残量:10mL\n')
    assert result['identified'] and result['limited'] and result['events'] == []


def test_unknown_measurement_date_does_not_borrow_document_date():
    row, = _project('PCAポンプ\n測定日:不明 作成日:2026年11月5日 残量:10mL\n')['events']
    assert row['time'] is None and row['dates'] == []


def test_continuation_page_is_kept_as_unverified_without_borrowed_pump_context():
    result = pump_document.project([{'page': 1, 'text': 'PCAポンプ\n残量:10mL\n'},
                                    {'page': 2, 'text': '残量:9mL\n'}])
    assert len(result['events']) == 2
    assert result['events'][1]['page'] == 2
    assert result['events'][1]['candidate_status'] == 'needs_review'
    assert 'number' not in result['events'][1] and result['events'][1]['unverified']
