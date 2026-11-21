import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts/development"
sys.path.insert(0, str(SCRIPTS))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


module = _load("generate_release_video")


def _spec(tmp_path, scenes):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps({"scenes": scenes}), encoding="utf-8")
    return path


def test_shipped_spec_validates():
    spec = module.load_spec(module.DEFAULT_SPEC)
    assert len(spec["scenes"]) == 12
    assert {scene["type"] for scene in spec["scenes"]} == {"card", "gallery", "image"}


@pytest.mark.parametrize("scene", [
    {"type": "bogus", "seconds": 3},
    {"type": "card", "title": "t"},
    {"type": "card", "title": "t", "seconds": 0},
    {"type": "card", "title": "t", "seconds": -1},
    {"type": "card", "title": "t", "seconds": True},
    {"type": "card", "title": "t", "seconds": 3, "lines": "not-a-list"},
    {"type": "card", "title": "t", "seconds": 3, "lines": ["ok"] * 10},
    {"type": "card", "title": "  ", "seconds": 3},
    {"type": "gallery", "gallery": "slack", "screen": "99-nope", "seconds": 3},
    {"type": "gallery", "gallery": "discord", "screen": "01-x", "seconds": 3},
    {"type": "image", "seconds": 3},
    {"type": "image", "path": "docs/screenshots/does-not-exist.png", "seconds": 3},
])
def test_load_spec_rejects_invalid_scenes(tmp_path, scene):
    with pytest.raises(ValueError):
        module.load_spec(_spec(tmp_path, [scene]))


def test_card_svg_escapes_markup_and_fits_long_titles():
    svg = module.card_svg("A&B <test>", ["line1", "line2"])
    assert "A&amp;B &lt;test&gt;" in svg
    assert "<test>" not in svg
    long_title = module.card_svg("あ" * 30, [])
    assert 'font-size="76"' not in long_title


def test_segment_command_shape():
    args = module.segment_command("/tmp/a.png", "/tmp/a.m4a", 6.5, "/tmp/seg.mp4")
    assert args[:2] == ["ffmpeg", "-y"]
    assert "-loop" in args and "1" in args
    assert "libx264" in args
    assert any("format=yuv420p" in arg for arg in args)
    assert "apad=whole_dur=6.500" in args
    assert args[-1] == "/tmp/seg.mp4"


def test_concat_command_lists_segments():
    list_path = Path("/tmp/segments.txt")
    args = module.concat_command(["/tmp/s0.mp4", "/tmp/s1.mp4"], list_path, "/tmp/out.mp4")
    assert "-f" in args and "concat" in args and "-c" in args and "copy" in args
    assert args[-1] == "/tmp/out.mp4"
