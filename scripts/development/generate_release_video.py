#!/usr/bin/env python3
"""Assemble gallery screenshots and title cards into a narrated release video."""

import argparse
from html import escape
import importlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SHOTS = ROOT / "docs/screenshots"
GALLERIES = {
    "slack": ("generate_slack_gallery", SHOTS / "slack-gallery"),
    "lineworks": ("generate_lineworks_gallery", SHOTS / "lineworks-gallery"),
}
SCENE_TYPES = ("card", "gallery", "image")
CANVAS_W, CANVAS_H, FPS = 1080, 1920, 30
INK, MUTED = "#202124", "#9aa0a6"
FONT = "Noto Sans CJK JP,Hiragino Sans,Yu Gothic,Droid Sans Fallback,sans-serif"
DEFAULT_SPEC = SHOTS / "release-video.json"
DEFAULT_OUT = SHOTS / "release-video.mp4"
AUDIO_TAIL_S = 0.5
MAX_CARD_LINES = 9


def _width_units(text):
    """CJK glyphs count double; result approximates glyph-em width."""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def _fit_size(texts, max_width, base):
    widest = max(_width_units(t) for t in texts) or 1
    return max(24, min(base, int(max_width * 2 / widest)))


def card_svg(title, lines):
    title_size = _fit_size([title], CANVAS_W * 0.88, 76)
    line_size = _fit_size(lines, CANVAS_W * 0.8, 44) if lines else 0
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{CANVAS_W}" height="{CANVAS_H}" '
        f'viewBox="0 0 {CANVAS_W} {CANVAS_H}">',
        f'<rect width="{CANVAS_W}" height="{CANVAS_H}" fill="{INK}"/>',
        f'<text x="{CANVAS_W // 2}" y="720" text-anchor="middle" font-family="{FONT}" '
        f'font-size="{title_size}" font-weight="bold" fill="#ffffff">{escape(title)}</text>',
    ]
    y = 900
    for line in lines:
        parts.append(
            f'<text x="{CANVAS_W // 2}" y="{y}" text-anchor="middle" font-family="{FONT}" '
            f'font-size="{line_size}" fill="{MUTED}">{escape(line)}</text>')
        y += 88
    parts.append("</svg>")
    return "\n".join(parts)


def _gallery_module(name):
    module_name, _ = GALLERIES[name]
    return importlib.import_module(module_name)


def _validate_scene(scene, index):
    where = f"scene {index}"
    if not isinstance(scene, dict):
        raise ValueError(f"{where}: not an object")
    kind = scene.get("type")
    if kind not in SCENE_TYPES:
        raise ValueError(f"{where}: unknown type {kind!r}")
    seconds = scene.get("seconds")
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds <= 0:
        raise ValueError(f"{where}: seconds must be a positive number")
    narration = scene.get("narration", "")
    if not isinstance(narration, str):
        raise ValueError(f"{where}: narration must be a string")
    if kind == "card":
        if not isinstance(scene.get("title"), str) or not scene["title"].strip():
            raise ValueError(f"{where}: card needs a title")
        lines = scene.get("lines", [])
        if (not isinstance(lines, list) or len(lines) > MAX_CARD_LINES
                or not all(isinstance(line, str) and line.strip() for line in lines)):
            raise ValueError(f"{where}: card lines must be up to {MAX_CARD_LINES} non-empty strings")
    elif kind == "gallery":
        gallery, screen = scene.get("gallery"), scene.get("screen")
        if gallery not in GALLERIES:
            raise ValueError(f"{where}: unknown gallery {gallery!r}")
        if screen not in _gallery_module(gallery).SCREENS:
            raise ValueError(f"{where}: unknown {gallery} screen {screen!r}")
        if not _frame_path(scene).is_file():
            raise ValueError(f"{where}: missing PNG {_frame_path(scene)}; "
                             f"run scripts/development/{GALLERIES[gallery][0]}.py --png")
    elif kind == "image":
        path = scene.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError(f"{where}: image needs a path")
        if not _frame_path(scene).is_file():
            raise ValueError(f"{where}: missing image {path}")


def load_spec(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("spec must be a JSON object")
    voice, rate = data.get("voice", "Kyoko"), data.get("rate", 190)
    scenes = data.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("spec needs a non-empty scenes list")
    if not isinstance(voice, str) or not voice:
        raise ValueError("voice must be a non-empty string")
    if not isinstance(rate, (int, float)) or isinstance(rate, bool) or rate <= 0:
        raise ValueError("rate must be a positive number")
    for index, scene in enumerate(scenes):
        _validate_scene(scene, index)
    return {"voice": voice, "rate": rate, "scenes": scenes}


def _frame_path(scene):
    kind = scene["type"]
    if kind == "gallery":
        return GALLERIES[scene["gallery"]][1] / f"{scene['screen']}.png"
    if kind == "image":
        return ROOT / scene["path"]
    raise ValueError("card scenes have no static frame")


def render_card(scene, index, workdir):
    svg = workdir / f"card-{index:02d}.svg"
    png = workdir / f"card-{index:02d}.png"
    svg.write_text(card_svg(scene["title"], scene.get("lines", [])), encoding="utf-8")
    subprocess.run(["sips", "-s", "format", "png", "--resampleWidth", str(CANVAS_W),
                    str(svg), "--out", str(png)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return png


def narrate(text, out, voice, rate):
    subprocess.run(["say", "-v", voice, "-r", str(rate), "-o", str(out), text],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def silence(seconds, out):
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                    "-t", f"{seconds:.3f}", "-c:a", "aac", str(out)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def audio_seconds(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], check=True,
                         capture_output=True, text=True)
    return float(out.stdout.strip())


def segment_command(png, audio, duration, out):
    vf = (f"scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=decrease,"
          f"pad={CANVAS_W}:{CANVAS_H}:(ow-iw)/2:(oh-ih)/2:color=0x{INK[1:]},format=yuv420p")
    return ["ffmpeg", "-y", "-loop", "1", "-framerate", str(FPS), "-i", str(png),
            "-i", str(audio), "-t", f"{duration:.3f}", "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-af", f"apad=whole_dur={duration:.3f}", "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", str(out)]


def concat_command(segments, list_path, out):
    list_path.write_text("".join(f"file '{segment}'\n" for segment in segments),
                         encoding="utf-8")
    return ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-c", "copy", str(out)]


def _required_tools(spec):
    tools = ["ffmpeg", "ffprobe"]
    if any(scene["type"] == "card" for scene in spec["scenes"]):
        tools.append("sips")
    if any(scene.get("narration", "").strip() for scene in spec["scenes"]):
        tools.append("say")
    return tools


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--check", action="store_true",
                        help="validate the spec and referenced frames without rendering")
    args = parser.parse_args()
    spec = load_spec(args.spec)
    if args.check:
        print(f"{len(spec['scenes'])} scenes validated ({args.spec})")
        return 0
    missing = [tool for tool in _required_tools(spec) if shutil.which(tool) is None]
    if missing:
        print(f"release video: missing tools: {', '.join(missing)}", file=sys.stderr)
        return 1
    workdir = Path(tempfile.mkdtemp(prefix="release-video-"))
    try:
        segments = []
        for index, scene in enumerate(spec["scenes"]):
            png = render_card(scene, index, workdir) if scene["type"] == "card" else _frame_path(scene)
            audio = workdir / f"audio-{index:02d}.m4a"
            narration = scene.get("narration", "").strip()
            if narration:
                aiff = workdir / f"voice-{index:02d}.aiff"
                narrate(narration, aiff, spec["voice"], spec["rate"])
                subprocess.run(["ffmpeg", "-y", "-i", str(aiff), "-c:a", "aac", str(audio)],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                duration = max(scene["seconds"], audio_seconds(audio) + AUDIO_TAIL_S)
            else:
                silence(scene["seconds"], audio)
                duration = scene["seconds"]
            segment = workdir / f"segment-{index:02d}.mp4"
            subprocess.run(segment_command(png, audio, duration, segment), check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            segments.append(segment)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        list_path = workdir / "segments.txt"
        subprocess.run(concat_command(segments, list_path, args.out), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print(f"wrote {args.out} ({len(segments)} scenes)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"release video: {exc}", file=sys.stderr)
        raise SystemExit(1)
