"""Install the call transport under .runtime, without changing global Node/Git."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent.parent
NODE_VERSION = "24.21.0"


def node_executable() -> Path | None:
    custom = os.environ.get("ANIMA_DISCORD_NODE", "")
    choices = [custom, str(ROOT / ".runtime" / "discord-node" / "node.exe"), shutil.which("node")]
    for choice in choices:
        if not choice:
            continue
        try:
            version = subprocess.check_output([choice, "--version"], text=True, timeout=10).strip()
            if tuple(int(n) for n in version.lstrip("v").split(".")) >= (22, 12, 0):
                return Path(choice).resolve()
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return None


def install_windows_node() -> Path:
    if sys.platform != "win32":
        raise RuntimeError("Install Node.js 22.12 or newer, then run this setup again.")
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    name = f"node-v{NODE_VERSION}-win-{arch}"
    base = f"https://nodejs.org/dist/v{NODE_VERSION}/"
    print(f"Downloading a private Node.js {NODE_VERSION} runtime from nodejs.org…", flush=True)
    sums = urllib.request.urlopen(base + "SHASUMS256.txt", timeout=60).read().decode()
    expected = next(line.split()[0] for line in sums.splitlines() if line.split()[-1] == name + ".zip")
    data = urllib.request.urlopen(base + name + ".zip", timeout=180).read()
    if hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError("Node download failed its official SHA-256 check.")
    import io
    target = ROOT / ".runtime" / "discord-node"
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            parts = Path(info.filename).parts
            if not parts or parts[0] != name or info.is_dir():
                continue
            dest = (target / Path(*parts[1:])).resolve()
            if not dest.is_relative_to(target.resolve()):
                raise RuntimeError("Unexpected path in Node archive.")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, dest.open("wb") as dst:
                shutil.copyfileobj(src, dst)
    return target / "node.exe"


def main():
    node = node_executable() or install_windows_node()
    folder = ROOT / ".runtime" / "discord-voice"
    folder.mkdir(parents=True, exist_ok=True)
    engine = Path(__file__).resolve().parent
    shutil.copyfile(engine / "discord_voice_package.json", folder / "package.json")
    lock = engine / "discord_voice_package-lock.json"
    if lock.is_file():
        shutil.copyfile(lock, folder / "package-lock.json")
    npm = node.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
    env = dict(os.environ, PATH=str(node.parent) + os.pathsep + os.environ.get("PATH", ""))
    if npm.is_file():
        cmd = [str(node), str(npm)]
    else:
        npm_cmd = shutil.which("npm")
        if not npm_cmd:
            raise RuntimeError("npm was not found alongside Node.js.")
        cmd = [npm_cmd]
    subprocess.run(cmd + ["ci" if lock.is_file() else "install", "--no-audit", "--no-fund"],
                   cwd=folder, env=env, check=True)
    if not lock.is_file():
        shutil.copyfile(folder / "package-lock.json", lock)
    print("Discord voice transport installed. No bot was connected.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Setup failed: {e}", file=sys.stderr)
        sys.exit(1)
