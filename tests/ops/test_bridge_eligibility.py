"""§6適格性評価: AUTO_ELIGIBLEの唯一の導出点 evaluate_eligibility。"""
import pytest

import release_auto_bridge

ALL = {name: True for name in release_auto_bridge._ELIGIBILITY_PREREQUISITES}


def test_all_prerequisites_measured_true_is_eligible():
    result = release_auto_bridge.evaluate_eligibility(dict(ALL))
    assert result == {'auto_eligible': True, 'blockers': []}


@pytest.mark.parametrize('missing', release_auto_bridge._ELIGIBILITY_PREREQUISITES)
def test_each_missing_prerequisite_blocks(missing):
    measurements = dict(ALL)
    del measurements[missing]
    result = release_auto_bridge.evaluate_eligibility(measurements)
    assert result['auto_eligible'] is False
    assert result['blockers'] == [missing]


@pytest.mark.parametrize('bad', [False, None, 0, 1, 'yes', 'true', [], {}])
def test_non_true_values_never_count_as_measured(bad):
    """True 以外の値(1/'yes'等)は測定済みとみなさない(fail-closed)。"""
    measurements = dict(ALL)
    measurements['profile_measured'] = bad
    result = release_auto_bridge.evaluate_eligibility(measurements)
    assert result['auto_eligible'] is False
    assert 'profile_measured' in result['blockers']


@pytest.mark.parametrize('bad_input', [None, [], 'x', 0, True])
def test_non_dict_input_blocks_everything(bad_input):
    result = release_auto_bridge.evaluate_eligibility(bad_input)
    assert result['auto_eligible'] is False
    assert result['blockers'] == list(release_auto_bridge._ELIGIBILITY_PREREQUISITES)


def test_unmanaged_processes_cleared_is_required():
    """AT090: 旧worker/別checkout/旧watchdogの未確認は適格性を落とす。"""
    measurements = dict(ALL)
    measurements['unmanaged_processes_cleared'] = False
    result = release_auto_bridge.evaluate_eligibility(measurements)
    assert result['auto_eligible'] is False
    assert result['blockers'] == ['unmanaged_processes_cleared']


def test_bridge_incomplete_blocks():
    """v1→v3 bridgeがverifiedまで完了していない限りAUTO_ELIGIBLEにしない。"""
    measurements = dict(ALL)
    measurements['bridge_completed'] = False
    assert release_auto_bridge.evaluate_eligibility(measurements)['auto_eligible'] is False


def test_extra_measurement_keys_are_ignored():
    measurements = dict(ALL, unrelated_probe='anything')
    result = release_auto_bridge.evaluate_eligibility(measurements)
    assert result['auto_eligible'] is True
