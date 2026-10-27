"""Written schedule mentions remain source-scoped in patient views."""
import notify_views
from semantic_testkit import _message
from test_patient_context_v3_views import T0
from test_patient_context_v3_views import ctx as ctx


def test_three_unassigned_mentions_are_visible_without_creating_interventions(ctx):
    bodies = ['明日に介護ベッドを設置します', '11/2に訪問調査です', '来週金曜日に担当者会議']
    ctx.db.save_messages([_message(2001 + i, body=body) for i, body in enumerate(bodies)], project_id=1)
    ctx.db.ensure_patient(2)
    ctx.db.save_messages([_message(2010, pid=2, body='明日に介護ベッド設置')], project_id=2)
    first = notify_views.patient_context_list(ctx.db.db, 1, 'previsit', cfg={}, since=T0, now=T0)
    items = list(first['items'])
    for page in range(1, first['pages']):
        items.extend(notify_views.patient_context_list(ctx.db.db, 1, 'previsit', cfg={},
                                                       since=T0, now=T0, page=page)['items'])
    mentions = [row for row in items if row['group'].startswith('予定に関する記載（対象・日付・担当未確認')]
    assert {quote for row in mentions for quote in row['quotes']} == set(bodies)
    assert {row['source_rest'] for row in mentions} == {'#2001', '#2002', '#2003'}
    assert all(row['project_id'] == 1 for row in mentions)
    assert not any(row['group'].startswith('今後の介入予定') and any(body in row['text'] for body in bodies)
                   for row in items)


def test_three_mentions_in_one_post_are_visible_in_same_unconfirmed_section(ctx):
    quotes = ['明日に介護ベッドを設置します。', '11/2に訪問調査です。', '来週金曜日に担当者会議。']
    ctx.db.save_messages([_message(2001, body=''.join(quotes))], project_id=1)
    first = notify_views.patient_context_list(ctx.db.db, 1, 'previsit', cfg={}, since=T0, now=T0)
    items = list(first['items'])
    for page in range(1, first['pages']):
        items.extend(notify_views.patient_context_list(ctx.db.db, 1, 'previsit', cfg={},
                                                       since=T0, now=T0, page=page)['items'])
    rows = [row for row in items if row['group'].startswith('予定に関する記載（対象・日付・担当未確認')]
    assert {quote for row in rows for quote in row['quotes']} == set(quotes)
    assert all(row['source_rest'] == '#2001' and row['project_id'] == 1 for row in rows)
