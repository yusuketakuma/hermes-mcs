"""Error type shared by housekeeping and local backup with a stable reason token."""


class MaintenanceError(RuntimeError):
    """Housekeeping failure carrying a short stable reason token
    (e.g. "backup_verify_failed"). Distinct from the adapter's MCSError
    — core must not depend on the ingest layer for an error type."""
