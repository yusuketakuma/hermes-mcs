from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
GUIDES = ROOT / "docs" / "guides"
RAW_MCS_PYTHON = re.compile(r"(?<![\w/.-])python3(?=\s+mcs/(?:ops|views)/)")


def test_operator_guides_do_not_use_macos_system_python_for_mcs_commands():
    offenders = []
    for path in sorted(GUIDES.rglob("*.md")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if RAW_MCS_PYTHON.search(line):
                offenders.append(f"{path.name}:{number}")
    assert offenders == []


def test_readme_manual_sections_do_not_use_macos_system_python_for_mcs_commands():
    text = (ROOT / "README.md").read_text()
    generated = re.search(
        r"<!-- BEGIN GENERATED:release -->.*?<!-- END GENERATED:release -->",
        text, re.S)
    assert generated
    manual = text[:generated.start()] + text[generated.end():]
    assert not RAW_MCS_PYTHON.search(manual)
    assert 'PY="$HOME/.mcs/venv/bin/python3"' in manual
    assert '"$PY" mcs/ops/mcs_compliance.py acknowledge' in manual


def test_guides_using_py_define_and_quote_the_runtime_specific_interpreter():
    missing = []
    unquoted = []
    for path in sorted(GUIDES.rglob("*.md")):
        text = path.read_text()
        if '"$PY"' not in text:
            continue
        if "MCS用Python" not in text and "MCS のスクリプト" not in text \
                and not re.search(r"PY\s*=\s*[\"']?[^\n]*(\.mcs/venv|hermes-agent/venv)", text):
            missing.append(path.name)
        for number, line in enumerate(text.splitlines(), 1):
            if re.search(r"(?<![\w\"'])\$PY(?=\s)", line):
                unquoted.append(f"{path.name}:{number}")
    assert missing == []
    assert unquoted == []


def test_lineworks_text_only_setup_is_not_described_as_requiring_callback_residency():
    lineworks = (GUIDES / "LINEWORKS.md").read_text()
    standalone = (GUIDES / "STANDALONE.md").read_text()
    easy = (GUIDES / "EASY_SETUP.md").read_text()
    assert "文章だけの通知では Callback URL は不要です" in lineworks
    assert "文章だけの通知ではアダプターの常駐は不要です" in standalone
    assert "認証・受信URL・常駐設定も完了してから" not in easy


def _github_anchor(title):
    title = re.sub(r"<[^>]+>", "", title)
    title = re.sub(r"[`*_~\[\]()]", "", title).strip().lower()
    out = []
    import unicodedata
    for char in title:
        if char.isspace():
            out.append("-")
        elif char in "-_" or not unicodedata.category(char).startswith(("P", "S")):
            out.append(char)
    return "".join(out)


def _anchors(path):
    seen = {}
    found = set()
    for line in path.read_text().splitlines():
        match = re.match(r"#{1,6}\s+(.+?)\s*#*$", line)
        if match:
            base = _github_anchor(match.group(1))
            index = seen.get(base, 0)
            seen[base] = index + 1
            found.add(base if index == 0 else f"{base}-{index}")
        found.update(re.findall(r'<a (?:name|id)="([^"]+)"', line))
    return found


def test_operator_doc_local_links_and_anchors_resolve():
    import urllib.parse
    broken = []
    for source in [ROOT / "README.md", *sorted(GUIDES.rglob("*.md"))]:
        for match in re.finditer(r"\[[^\]\n]+\]\(([^)\s]+)\)", source.read_text()):
            target = match.group(1)
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            name, _, anchor = target.partition("#")
            destination = (source.parent / urllib.parse.unquote(name)).resolve() \
                if name else source.resolve()
            label = f"{source.relative_to(ROOT)} -> {target}"
            if name and not destination.is_file():
                broken.append(label)
            elif anchor and urllib.parse.unquote(anchor) not in _anchors(destination):
                broken.append(label)
    assert broken == []
