"""Project-local Rubber Band R3 installation for pitch-preserving Kyutai delivery."""
from pathlib import Path
import hashlib,subprocess,sys,urllib.request,zipfile,io,shutil
ROOT=Path(__file__).resolve().parent.parent
URL='https://breakfastquay.com/files/releases/rubberband-4.0.0-gpl-executable-windows.zip'
HASH='f2d47fc64dbb42f6cc62edf7933ac4fa89d8f0ef8b9cf97b6afc263a7fe05644'
def main():
    if sys.platform!='win32':raise SystemExit('Install Rubber Band 4.x for your platform and adapt the private binary path; this installer currently targets Windows.')
    root=ROOT/'.runtime/rubberband';root.mkdir(parents=True,exist_ok=True)
    archive=root/'release.zip'
    if not archive.exists():
        with urllib.request.urlopen(URL,timeout=60) as r:archive.write_bytes(r.read(30_000_000))
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=HASH:raise RuntimeError('Rubber Band archive checksum mismatch.')
    with zipfile.ZipFile(archive) as z:
        for item in z.infolist():
            target=(root/item.filename).resolve()
            if not target.is_relative_to(root.resolve()):raise RuntimeError('Invalid tool archive path.')
            if not item.is_dir():z.extract(item,root)
    python=ROOT/'.runtime/kyutai-env/Scripts/python.exe';uv=ROOT/'.runtime/installer/bin/uv.exe'
    if uv.is_file():cmd=[str(uv),'pip','install','--python',str(python),'soxr']
    else:cmd=[str(python),'-m','pip','install','soxr']
    subprocess.run(cmd,check=True)
    print('Kyutai whisper and pitch-preserving delivery tools are ready.')
if __name__=='__main__':main()
