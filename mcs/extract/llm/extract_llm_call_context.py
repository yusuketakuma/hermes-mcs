"""Own shared per-thread transport context and the process background slot."""
import contextlib
import threading
import bounded_http
import cloud_inference
import local_llm


def _opener_request(endpoint: str, method: str, body, timeout: float,
                    deadline: float | None = None):
    """Bounded loopback transport shared by generation and health probes."""
    guard = getattr(_CALL_NOTES, "medication_guard", None)
    try:
        current = guard is None or guard()
    except cloud_inference.CloudFallback as error:
        if str(error) != "cloud_owner_stopped":
            raise
        current = False
    if not current:
        _CALL_NOTES.medication_source_changed = True
        raise bounded_http.NotSentTimeout("medication_reference_changed")
    with getattr(_CALL_NOTES, "transport_context", contextlib.nullcontext)():
        return local_llm.bounded_request(endpoint, method, body, timeout, deadline)


_CALL_NOTES = threading.local()


_SLOT_OVERRIDE = None  # --lane: this process's backlog wire id_slot


def _choose_slot() -> int:
    """Pin each process to its configured lane; never borrow realtime."""
    if _SLOT_OVERRIDE in local_llm.BACKGROUND_SLOTS \
            and type(_SLOT_OVERRIDE) is int:
        return _SLOT_OVERRIDE
    if _SLOT_OVERRIDE is not None:
        return local_llm.BACKGROUND_SLOT
    return local_llm.request_slot()
