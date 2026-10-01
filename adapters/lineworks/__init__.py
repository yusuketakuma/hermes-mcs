"""Independent LINE WORKS Bot adapter using the official API."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcs"))
import _mcs_path  # noqa: E402,F401  reuse the existing flat module roots

from .client import ClientError, Credentials, LineWorksClient, verify_signature  # noqa: E402

__all__ = ["ClientError", "Credentials", "LineWorksClient", "verify_signature"]
