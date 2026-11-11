"""v3 local consent revoke確認deps面。

deps契約 `local_revoke() -> bool` は同意撤回の最新確認。Trueは撤回または
不明を意味し、ownerはCONSENT_REVOKED/HOLD_PRESENTでfail-closedする。

authoritative sourceはowner stateのconsent記録。trusted CLI `auto-disable`が
A/O lock経由でenabled=false・local_consent_epoch増分を書く設計(02 §revocation)。
本moduleは呼出毎にauto_state.loadで最新状態を読み、cacheしない。
consent欠落・破損・enabled!=true・state読取不能は全てTrue(撤回/不明扱い)。
expiryは_consent_live側の責務なのでここでは見ない — enabled=trueの期限切れは
撤回ではなくCONSENT_REQUIREDとして別経路で止まる。
"""
from adapters.sdc import release_auto_state as auto_state


class LocalRevoke:
    """owner state consent記録から撤回を判定するdeps実装。"""

    def __init__(self, store):
        """store: auto_state.loadが読めるStore互換(read/root_fingerprint)、
        または呼出毎に新しいStoreを返すcallable(factoryは専用の読取Storeを
        都度開閉するためこちらを使う)。lock区間内外どちらからも呼ばれる。"""
        self._store = store

    def _owner(self):
        if callable(self._store):
            s = self._store()
            try:
                return auto_state.load(s)
            finally:
                try:
                    s.close()
                except Exception:
                    pass
        return auto_state.load(self._store)

    def __call__(self):
        try:
            owner = self._owner()
        except Exception:
            return True
        consent = owner.get('consent') if isinstance(owner, dict) else None
        if not isinstance(consent, dict):
            return True
        return consent.get('enabled') is not True
