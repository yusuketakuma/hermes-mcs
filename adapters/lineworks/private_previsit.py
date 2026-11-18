"""Verified LINE WORKS one-user delivery for private previsit notes."""
from __future__ import annotations

from pathlib import Path

from .client import ClientError
from .config import load_credentials, settings
from .delivery import Sender, failure


class PrivatePrevisitProvider:
    def __init__(self, root):
        self.root = str(Path(root).expanduser().resolve())

    @staticmethod
    def _matches(scope, account_scope, project_id, recipient_id):
        if not isinstance(scope, dict) or not isinstance(account_scope, dict):
            return False
        if (not isinstance(scope.get("profile"), str)
                or not isinstance(scope.get("application_id"), str)
                or not isinstance(scope.get("team_id"), str)
                or type(scope.get("route_epoch")) is not int
                or not isinstance(scope.get("allowed_user_ids"), (set, frozenset))
                or not isinstance(scope.get("project_ids"), (set, frozenset))):
            return False
        expected = {key: scope.get(key) for key in
                    ("profile", "application_id", "team_id", "route_epoch")}
        if expected != account_scope:
            return False
        return (type(project_id) is int and project_id in scope["project_ids"]
                and isinstance(recipient_id, str) and bool(recipient_id.strip())
                and recipient_id in scope["allowed_user_ids"])

    def send(self, content, *, project_id, recipient_id, transport, account_scope,
             verify_authorization=None):
        if transport != "lineworks":
            return {"result": "not_sent", "error_code": "unsupported_transport"}
        if not isinstance(content, str) or not content or not recipient_id:
            return {"result": "not_sent", "error_code": "validation_invalid"}
        try:
            current = settings(self.root, require_interactive=False)
        except (ClientError, OSError, ValueError, TypeError):
            return {"result": "not_sent", "error_code": "private_verification_failed"}
        if not self._matches(current, account_scope, project_id, recipient_id):
            return {"result": "not_sent", "error_code": "private_verification_failed"}
        try:
            client, _ = load_credentials(self.root, current)
        except (ClientError, OSError, ValueError, TypeError):
            return {"result": "not_sent", "error_code": "private_provider_unavailable"}
        if getattr(getattr(client, "credentials", None), "bot_id", None) != current["application_id"]:
            return {"result": "not_sent", "error_code": "private_verification_failed"}

        def verify_current():
            live = settings(self.root, require_interactive=False)
            return (self._matches(live, account_scope, project_id, recipient_id)
                    and (verify_authorization is None or verify_authorization() is True))

        sender = Sender(client, current, current["data_root"])
        try:
            sender.send({"type": "text", "text": content}, user_id=recipient_id,
                        verify_scope=verify_current)
        except Exception as exc:
            outcome = failure(exc)
            if isinstance(exc, ClientError) and isinstance(exc.status, int) \
                    and 400 <= exc.status < 500 and exc.status != 429:
                return {"result": "not_sent", "error_code": "http_4xx_rejected"}
            return outcome
        return {"result": "delivered", "accepted_ref": "lineworks:201"}
