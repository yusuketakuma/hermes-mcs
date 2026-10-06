"""Discord ephemeral answers render source text literally; sealed
attachments are confined to the data root's attachments dir. Synthetic
data only."""
import hashlib

from adapters.common import paths, text


def test_discord_body_answer_has_no_active_markdown():
    answer = text.view_answer(
        {"outcome": "applied", "action": "body", "title": "山田**花子",
         "body": "[MCSで確認](https://evil.example/x)\n# 至急\n||隠し||"},
        lambda pid: True)
    msg = answer[0][0]
    heading, *lines = msg.split("\n")
    assert heading == "**山田＊＊花子**"
    assert lines == ["［MCSで確認］(https://evil.example/x)", "＃ 至急", "｜｜隠し｜｜"]


def test_escape_keeps_body_budget():
    body = "# 見出し\n" * 1000 + "#" * 3000
    out = text.body_messages({"title": "t", "body": body})
    plain = text.body_messages({"title": "t", "body": body}, plain=True)
    assert [len(m) for m in out] == [len(m) + 4 for m in plain]   # ** ** only
    # every chunk's lines, including a split line's remainder, are literal
    assert not any(line.startswith("#") for m in out for line in m.split("\n"))


def test_discord_list_and_tasks_escape_items_not_markup():
    result = {"outcome": "applied", "action": "list", "list": {
        "title": "🔎 a_b", "head": ["# 見出し"],
        "items": [{"project_id": 1, "group": "~~群~~", "text": "・||x|| https://e.example/a_b"}],
        "notes": ["> 引用"]}}
    out = text.view_answer(result, lambda pid: True)[0][0].split("\n")
    assert out == ["**🔎 a＿b**", "＃ 見出し", "■ ～～群～～",
                   "・｜｜x｜｜ https://e.example/a_b", "＞ 引用"]
    tasks = text.view_answer({"outcome": "applied", "action": "tasks", "tasks": [
        {"request_id": 1, "status": "open", "title": "[x](https://e.example)",
         "assignee": "**誰**"}]}, lambda pid: True)[0][0]
    assert tasks.endswith("#1 ［x］(https://e.example) — 担当: ＊＊誰＊＊")


def test_plain_dialects_are_unchanged():
    result = {"outcome": "applied", "action": "body", "title": "a**b", "body": "# x"}
    assert text.view_answer(result, lambda pid: True, markdown=False,
                            plain=True)[0][0] == "a**b\n# x"


def _part(blob):
    return {"bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}


def test_attachment_confined_to_data_root(tmp_path):
    data = tmp_path / "data"
    (data / "attachments").mkdir(parents=True)
    inside = data / "attachments" / "a.bin"
    outside = tmp_path / "secret.bin"
    for f in (inside, outside):
        f.write_bytes(b"synthetic")
    part = _part(b"synthetic")
    assert paths.read_verified_attachment(str(inside), part, root=str(data)) == b"synthetic"
    assert paths.read_verified_attachment(str(outside), part, root=str(data)) is None
    # a symlink or ../ inside the dir resolving outside is refused
    link = data / "attachments" / "link.bin"
    link.symlink_to(outside)
    assert paths.read_verified_attachment(str(link), part, root=str(data)) is None
    dotted = str(data / "attachments" / ".." / ".." / "secret.bin")
    assert paths.read_verified_attachment(dotted, part, root=str(data)) is None
