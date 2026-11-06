#!/usr/bin/env python3
"""Install the optional OCR runtime without changing MCS's Python or configuration."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcs"))
import _mcs_path  # noqa: E402,F401
from ocr_model_contract import ENGINE_VERSION as VERSION, MODEL_JSON  # noqa: E402


def run(args, *, env):
    return subprocess.run(args, check=True, env=env, stdin=subprocess.DEVNULL)


def setup(root: Path, repo: Path):
    root = root.absolute()
    venv = root / 'ocr-venv'
    if venv.is_symlink():
        raise ValueError('OCR environment must not be a symlink')
    requirements = repo / 'deployment' / 'requirements-ocr.txt'
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
    marker = venv / '.mcs-ocr-requirements'
    python = venv / 'bin' / 'python'
    env = os.environ.copy()
    for name in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE',
                 'HF_HOME', 'HF_HUB_CACHE', 'HUGGINGFACE_HUB_CACHE', 'XDG_CACHE_HOME'):
        env.pop(name, None)
    env["HF_HOME"] = str(Path.home() / ".cache" / "huggingface")
    if not python.exists():
        if venv.exists():
            raise ValueError('Incomplete OCR environment; preserve it and choose a new location before retrying')
        uv = shutil.which('uv')
        if not uv:
            raise ValueError('uv is required to prepare the separate Python 3.13 OCR environment')
        run([uv, 'venv', '--python', '3.13', str(venv)], env=env)
    run([str(python), '-I', '-c',
         'import sys; assert sys.version_info[:2] == (3, 13), "OCR requires Python 3.13"'], env=env)
    dependencies = (digest + '\n' + VERSION + '\n').encode()
    expected = dependencies + (MODEL_JSON + '\n').encode()
    if not marker.exists() or not marker.read_bytes().startswith(dependencies):
        uv = shutil.which('uv')
        if not uv:
            raise ValueError('uv is required to install the optional OCR dependencies')
        run([uv, 'pip', 'install', '--python', str(python), '--reinstall', '-r', str(requirements)], env=env)
    run([str(python), '-I', '-c',
         f'from importlib.metadata import version; assert version("yomitoku") == {VERSION!r}'], env=env)
    # Local-only lookup needs each weight, since a snapshot directory alone can be incomplete.
    run([str(python), '-I', '-c',
         "from huggingface_hub import snapshot_download, hf_hub_download; from huggingface_hub.errors import LocalEntryNotFoundError; import sys, json\n"
         "for repo, revision in json.loads(sys.argv[1]).values():\n"
         "    try:\n"
         "        hf_hub_download(repo_id=repo, filename='model.safetensors', revision=revision, local_files_only=True)\n"
         "    except LocalEntryNotFoundError:\n"
         "        snapshot_download(repo_id=repo, revision=revision)\n"
         "        hf_hub_download(repo_id=repo, filename='model.safetensors', revision=revision, local_files_only=True)\n", MODEL_JSON], env=env)
    if not (venv / 'bin' / 'yomitoku').is_file():
        raise ValueError('OCR executable was not installed')
    marker.write_bytes(expected)
    print(f'OCR runtime ready: {venv / "bin" / "yomitoku"}')
    print('OCR remains disabled until attachment_ocr is configured. Model licence: noncommercial use only.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    try:
        setup(args.root, Path(__file__).resolve().parent.parent)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f'OCR installation failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
