"""Install voice dependencies in a project-local Python; never reset the friend.

    python engine/setup_voice.py             install isolated environment
    python engine/setup_voice.py --download  also download/warm models
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(args, env):
    print("Running: " + " ".join(str(a) for a in args), flush=True)
    subprocess.run([str(a) for a in args], cwd=ROOT, env=env, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true")
    backend = parser.add_mutually_exclusive_group()
    backend.add_argument("--qwen", action="store_true", help="install the separate native Qwen voice environment")
    backend.add_argument("--kyutai", action="store_true", help="install Kyutai TTS 1.6B in a separate environment")
    args = parser.parse_args()
    env = dict(os.environ, UV_CACHE_DIR=str(ROOT / ".runtime" / "uv-cache"),
               UV_PYTHON_INSTALL_DIR=str(ROOT / ".runtime" / "python"))
    bootstrap = ROOT / ".runtime" / "installer"
    uv = bootstrap / ("bin/uv.exe" if os.name == "nt" else "bin/uv")
    if not uv.exists():
        run([sys.executable, "-m", "pip", "install", "--target", bootstrap, "uv"], env)
    selected = "kyutai" if args.kyutai else "qwen" if args.qwen else "chatterbox"
    home = ROOT / {"qwen": ".runtime/voice-design-env", "kyutai": ".runtime/kyutai-env"}.get(selected, ".venv-voice")
    python = home / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        run([uv, "venv", home, "--python", "3.12", "--managed-python"], env)
    # Chatterbox pins 2.6 upstream. Override only inside this environment,
    # using CUDA 12.8 wheels which support Blackwell GPUs such as the 5090.
    indexes = ["--index", "pytorch=https://download.pytorch.org/whl/cu128", "--index-strategy", "unsafe-best-match"] if os.name == "nt" or sys.platform == "linux" else []
    overrides = [] if selected != "chatterbox" else ["--override", ROOT / "requirements-voice-overrides.txt"]
    requirements = ROOT / {"qwen": "requirements-voice-design.txt", "kyutai": "requirements-voice-kyutai.txt"}.get(selected, "requirements-voice.txt")
    run([uv, "pip", "install", "--python", python, *overrides, *indexes, "-r", requirements], env)
    if selected=='kyutai' and sys.platform=='win32':
        subprocess.run([sys.executable,str(ROOT/'engine/setup_delivery.py')],check=True)
    if args.download:
        import live_voice
        try:
            print(live_voice.WORKER.request("download", backend=selected), flush=True)
        finally:
            live_voice.WORKER.close()
    print("Voice environment ready. Open the parlor, choose a voice sample if requested, then Start voice.")


if __name__ == "__main__":
    try:
        main()
    except (subprocess.CalledProcessError, OSError, RuntimeError) as e:
        print(f"Voice setup stopped: {e}", file=sys.stderr)
        sys.exit(1)
