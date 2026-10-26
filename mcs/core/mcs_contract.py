"""Stable MCS message values, identifiers and structured acquisition errors."""
from __future__ import annotations

from dataclasses import dataclass, field

BASE = "https://www.medical-care.net"
API = f"{BASE}/api/v2t"


def project_url(project_id: int) -> str:
    """The MCS web page of a patient project (medical room)."""
    return f"{BASE}/projects/medical/{project_id}"



class MCSError(Exception):
    """Structured, log-safe error: kind/status/retryable only — never bodies."""
    def __init__(self, kind: str, detail: str = "", status: int | None = None,
                 retryable: bool = False):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.status = status
        self.retryable = retryable
        # read_thread attaches IDs from validated read responses before a
        # later failure; callers must persist their acquisition intent even
        # when this exception (including SessionExpired) aborts the walk.
        self.observed_reply_ids: frozenset[int] = frozenset()



class BootstrapError(MCSError):
    def __init__(self, detail: str):
        super().__init__("bootstrap_error", detail)


class SessionExpired(MCSError):
    def __init__(self, detail: str = "", status: int | None = None):
        super().__init__("session_expired", detail, status, retryable=False)



class SchemaError(MCSError):
    def __init__(self, detail: str):
        super().__init__("schema_error", detail)



@dataclass
class Attachment:
    file_id: str
    name: str
    url: str



@dataclass
class Message:
    message_id: int
    project_id: int
    parent_id: int | None
    sender_id: int | None
    sender_name: str
    sender_type: str
    profession: str
    organization: str
    posted_at: str
    body_html: str
    body_state: str            # unknown | snippet | full | deleted
    is_unread: bool
    reply_count: int
    replies: list[Message] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    # whether the response enumerated `files` at all — only a complete
    # list (possibly empty) may reconcile the stored attachment set;
    # an absent key means "not returned this call", never "no files"
    files_present: bool = False
    metadata: dict = field(default_factory=dict)
    metadata_errors: list[str] = field(default_factory=list)



@dataclass
class UnreadPatient:
    project_id: int
    project_type: str
    patient_name: str
    disease: str
    station_name: str
    url: str
    messages: list[Message] = field(default_factory=list)
    fetch_state: str = "pending"   # pending | complete | incomplete
    fetch_reason: str = ""
    # F-5: the unread route itself failed (server-side cap) but the
    # history walk certified every unread row — acknowledge through the
    # plain list read instead of the unread-filtered one
    ack_fallback: bool = False
    # MCS karte id (patient record) — the 連携サマリー route is keyed by it
    karte_id: int | None = None
    # epoch of the last message; only list_projects fills it (0 = unknown)
    last_activity: int = 0



@dataclass
class UnreadSnapshot:
    timestamp: int
    patients: list[UnreadPatient]
    listed_patients: list[UnreadPatient] = field(default_factory=list)



@dataclass
class MessageBatch:
    messages: list[Message]
    pages: int = 0
    reached: bool = False
    error: MCSError | None = None
    # the unread route showed its screen cap (UNREAD_SCREEN_CAP) or fewer
    # rows than paginate.total_entries — older unread rows exist that this
    # walk could not see, so the patient must not be acknowledged yet
    capped: bool = False
    # History-only, bounded page observations made before since filtering.
    page_observations: list[dict] | None = None



@dataclass
class ReplyBatch:
    messages: list[Message]
    missing: list[int]



def valid_id(v) -> bool:
    return type(v) is int and 0 < v < 2**63


def describe_error(e: Exception) -> str:
    """Describe structured acquisition failures without response bodies.

    MCSError.detail is internal structured text (e.g.
    'auto_login=keychain_locked') and is safe for logs/alerts."""
    if isinstance(e, MCSError):
        base = f"{e.kind}(status={e.status})" if e.status else e.kind
        detail = getattr(e, "detail", "")
        return f"{base}: {detail}" if detail else base
    return type(e).__name__
