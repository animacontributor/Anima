"""Install isolated audio analysis dependencies and optionally cache their models.

    python engine/setup_audio.py --download
    python engine/setup_audio.py --python .runtime/python/cpython-3.12-windows-x86_64-none/python.exe
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent


def run(command):
    subprocess.run([str(x) for x in command], cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", help="Python 3.12 to use for the isolated environment")
    parser.add_argument("--download", action="store_true", help="also cache Whisper, Audio Flamingo and Music Flamingo")
    args = parser.parse_args()
    import config
    if getattr(config, "OFFLINE", False):
        raise RuntimeError("Audio setup downloads packages; disable OFFLINE for setup.")
    windows = os.name == "nt"
    relative = Path(".runtime/audio-env") / ("Scripts/python.exe" if windows else "bin/python")
    python = ROOT / relative
    base = args.python
    if not base and windows:
        managed = ROOT / ".runtime/python/cpython-3.12-windows-x86_64-none/python.exe"
        if managed.is_file():
            base = str(managed)
    if not python.is_file():
        run([base or sys.executable, "-m", "venv", python.parent.parent])
    cache = ["--cache-dir", ROOT / ".runtime/pip-cache"]
    torch = ["torch==2.10.0", "--index-url", "https://download.pytorch.org/whl/cu128"] if windows or sys.platform == "linux" else ["torch"]
    run([python, "-m", "pip", "install", *torch, *cache])
    run([python, "-m", "pip", "install", "-r", ROOT / "requirements-audio.txt", *cache])
    import knobs
    _, refused, error = knobs.save(Path(config.__file__), {
        "EARS_PYTHON": relative.as_posix(), "MUSIC_EARS_PYTHON": relative.as_posix(),
    })
    if error or refused:
        raise RuntimeError(error or "Could not save audio interpreter settings")
    if args.download:
        run([python, Path(__file__).resolve(), "--cache-models"])
    print("Audio environment ready. Restart the panel, Parlor and bridges to use it.")


def cache_models():
    import config
    os.environ.setdefault("HF_HOME", str(ROOT / ".runtime/models"))
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    from faster_whisper import WhisperModel
    from huggingface_hub import snapshot_download
    WhisperModel(config.EARS_STT_MODEL, device="cpu", compute_type="int8",
                 download_root=str(ROOT / ".runtime/models/whisper"))
    snapshot_download(config.MUSIC_EARS_MODEL)
    snapshot_download(config.AUDIO_EARS_MODEL, allow_patterns=["*.json", "*.jinja", "*.safetensors", "*.model"])
    print("Speech, general audio and music models cached locally.")


if __name__ == "__main__":
    if sys.argv[1:] == ["--cache-models"]:
        cache_models()
    else:
        main()
