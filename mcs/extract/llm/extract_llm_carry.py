"""Settle carried extraction results under the current writer and source fences."""
import json
import time
import writer_lock

from extract_llm_errors import (
    _release,
    _renew,
    _write_lock,
)
from extract_llm_source_contract import (
    _CARRY_MARGIN_S,
    _RESIDENT_WRITE_WAIT_S,
    _owns_current_source,
    _writer_current,
)

def _settle_carry(ledger, carry: list, *, final: bool = True,
                  write: bool = True, fence=None, lock_context=None, report=True,
                  preserve_on_fence_loss=False, deadline=None):
    """Lock attempt for results carried across run_pending calls.
    ``final`` (resident exit) releases whatever is still lock-bound or
    no longer owned — it stays pending and re-extracts later, no attempt
    burned; a non-final pass (lane 3 before patient context) waits only
    the resident's short window and keeps lock-bound results carried.
    ``write=False`` (an updater ran) releases without committing so old
    code never writes after the update fence; ``fence`` (the resident's
    initial updater state) is re-read under the lock for the same
    reason — an updater may finish while we wait for the lock."""
    settled = dict.fromkeys(("done", "held_prior", "deferred"), 0)
    count, pids = 0, set()
    if not carry or writer_lock.writer_blocked():
        return settled, count, pids

    def _settle_note(reason):
        reasons = settled.setdefault("defer_reasons", {})
        reasons[reason] = reasons.get(reason, 0) + 1
    keep = []
    shadow = []
    context = lock_context if lock_context is not None else _write_lock(
        True, wait_s=60 if final else _RESIDENT_WRITE_WAIT_S)
    with context as held:
        if writer_lock.writer_blocked():
            return settled, count, pids
        if preserve_on_fence_loss and (not _writer_current(full=True) or
                (fence is not None and writer_lock.updater_state() != fence)):
            return settled, count, pids
        if held and fence is not None:
            write = write and writer_lock.updater_state() == fence
        next_index = 0
        try:
            for i, (r, lease, commit) in enumerate(carry):
                if deadline is not None and time.monotonic() >= deadline:
                    keep.extend(carry[i:])
                    break
                if held and write and _owns_current_source(ledger, r, lease):
                    before = settled['done']
                    commit(settled=settled, shadow=shadow)
                    next_index = i + 1
                    _release(ledger, r, lease)
                    count += 1
                    if settled['done'] > before:
                        pids.add(r['project_id'])
                elif final or held or lease <= time.time() + _CARRY_MARGIN_S:
                    if r.get("_cloud_discard") and (not write or not _owns_current_source(ledger, r, lease)):
                        r["_cloud_discard"]()
                    _release(ledger, r, lease)
                    if not report:
                        settled['deferred'] += 1
                        _settle_note("write_unavailable" if not held else
                                     "update_fence" if not write else "currentness_unverified")
                else:
                    renewed = _renew(ledger, r, lease)
                    if renewed is None:
                        if r.get("_cloud_discard"):
                            r["_cloud_discard"]()
                        _release(ledger, r, lease)
                        if not report:
                            settled['deferred'] += 1
                            _settle_note("lease_renewal_failed")
                    else:
                        # written back at once: a transient sqlite error on a
                        # later item leaves this list to the resident's retry,
                        # whose owner fence must match the renewed DB lease
                        carry[i] = (r, renewed, commit)
                        keep.append((r, renewed, commit))
                next_index = i + 1
        except BaseException:
            carry[:] = keep + carry[next_index:]
            raise
        # no comparison budget outside run_pending: these units keep the pending
        # row their commit wrote
    carry[:] = keep
    if report and any(settled.values()):
        print(json.dumps({"carry_settled": settled, "ts": time.time()}), flush=True)
    return settled, count, pids
