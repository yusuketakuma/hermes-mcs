"""Regression: LINE WORKS card attachments with quotes/backslashes upload under a valid name."""
from adapters.lineworks.client import valid_filename
from test_lineworks_adapter import world


def test_attachment_name_is_cleaned_for_upload(tmp_path):
    w = world(tmp_path)
    w.sender.attachment(b"synthetic", 'dir/a"b\\c\x01.pdf')
    name = w.client.calls[0][2]
    assert name == "a_b_c_.pdf" and valid_filename(name)
    w.sender.attachment(b"synthetic", "dir/")
    assert w.client.calls[-2][2] == "file"
