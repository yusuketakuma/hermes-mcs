"""v3 offer_eligible deps面: offer.selectedの端末側適格判定。

deps契約 `offer_eligible(offer, owner) -> bool`。stage_candidateが署名済み
Offerのschema検証後に呼び、Falseなら候補を棄却する(fail-closed)。

端末がこの時点で判定できるのは構造・bindingの整合だけ:
- offer.profile_sha256 == owner.profile.profile_sha256(SEM12 profile一致)
- selectedのpublication/digest各sha256・publication_id/rollout各idの形式
- rollout.authorization_epoch == offer.rollout_epoch(RolloutBinding一致)
- rollout.stageが5値enum、cohort/policy sha256・epoch/wave_indexの型

cohort所属・Compatibility内容・control closure・最小updater・forward
ancestryは中央署名とmetadata/artifact検証段階(SEM09以降・stage_verify・
apply)の責務であり、ここでは判定しない。例外・型不一致は全てFalse。
"""
import re

_STAGES = frozenset({'self', 'pilot', 'pct10', 'pct50', 'general'})
_HEX64 = re.compile(r'[0-9a-f]{64}')
_UUID = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_DIGEST_KEYS = ('manifest_digest', 'compatibility_sha256',
                'publication_proof_sha256', 'acceptance_sha256')


def _hex64(value):
    return type(value) is str and _HEX64.fullmatch(value) is not None


def _uuid(value):
    return type(value) is str and _UUID.fullmatch(value) is not None


class OfferEligible:
    """deps契約 `offer_eligible(offer, owner) -> bool` の実装。"""

    def __call__(self, offer, owner):
        try:
            return self._check(offer, owner)
        except Exception:
            return False

    def _check(self, offer, owner):
        if not isinstance(offer, dict) or not isinstance(owner, dict):
            return False
        profile = owner.get('profile')
        if (not isinstance(profile, dict)
                or offer.get('profile_sha256') != profile.get('profile_sha256')):
            return False
        selected = offer.get('selected')
        if not isinstance(selected, dict):
            return False
        if any(not _hex64(selected.get(key)) for key in _DIGEST_KEYS):
            return False
        if not _uuid(selected.get('publication_id')):
            return False
        rollout = selected.get('rollout')
        if not isinstance(rollout, dict):
            return False
        if (rollout.get('authorization_epoch') != offer.get('rollout_epoch')
                or rollout.get('stage') not in _STAGES
                or not _uuid(rollout.get('rollout_id'))
                or not _uuid(rollout.get('assignment_id'))
                or not _hex64(rollout.get('policy_sha256'))
                or not _hex64(rollout.get('cohort_sha256'))):
            return False
        return all(type(rollout.get(key)) is int and rollout[key] >= 0
                   for key in ('allocation_epoch', 'wave_index'))
