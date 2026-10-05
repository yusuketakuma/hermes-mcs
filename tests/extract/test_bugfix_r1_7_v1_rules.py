"""Regression tests for v1 rule extraction: full-width decimals,
negated urgent phrases, and explicit-year next-visit dates (all synthetic)."""
import pytest

import extract

POSTED = '2026-10-04T10:00:00+09:00'


@pytest.mark.parametrize('body,bt', [
    ('体温３８．２℃', 38.2), ('体温３９．５度', 39.5),
    ('体温38.2℃', 38.2), ('体温３８℃', 38)])
def test_full_width_temperature_keeps_decimal(body, bt):
    assert extract.extract_message(body, POSTED)['vitals']['bt'] == bt


@pytest.mark.parametrize('body,high', [
    ('搬送していません', False), ('救急搬送はしていません', False),
    ('搬送していない', False), ('緊急搬送はしておりません', False),
    ('救急搬送されていません', False), ('搬送されませんでした', False),
    ('至急ではなかった', False),
    ('救急搬送しています', True), ('至急搬送してください', True),
    ('急ぎではありませんが至急ご確認ください', True)])
def test_negated_urgent_forms(body, high):
    assert (extract.extract_message(body, POSTED).get('urgency') == 'high') is high


@pytest.mark.parametrize('body,want', [
    ('次回2026/10/20訪問', '2026-10-20'), ('次回2026/1/5', '2026-01-05'),
    ('次回 2010/12/3', '2010-12-03'), ('次回10/20訪問', '2026-10-20'),
    ('次回2026年10月20日訪問', '2026-10-20')])
def test_next_planned_explicit_year(body, want):
    assert extract.extract_message(body, POSTED).get('next_planned') == want
