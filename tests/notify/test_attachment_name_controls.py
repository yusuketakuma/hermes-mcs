"""Sealed attachment names must pass every sender's filename check."""
import hashlib

import notify_flush
from adapters.lineworks import client


def test_control_characters_in_attachment_names_are_replaced(tmp_path):
    path = tmp_path / "blob"
    path.write_bytes(b"synthetic")
    digest = hashlib.sha256(b"synthetic").hexdigest()
    names = ["report\tdoc.pdf", "bell\x07.png", "del\x7f.txt", 'q"uote\\.pdf', "line\nbreak.pdf"]
    att = {1: [{"attachment_id": i, "state": "downloaded", "local_path": str(path),
                "sha256": digest, "name": n, "file_id": None} for i, n in enumerate(names)]}
    files = notify_flush._collect_files(att, [1])
    assert [f for f, _ in files] == ["report_doc.pdf", "bell_.png", "del_.txt", "q_uote_.pdf",
                                     "line_break.pdf"]
    assert all(client.valid_filename(f) for f, _ in files)
