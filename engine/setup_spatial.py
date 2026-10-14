"""Install optional desktop rendering in a private environment and fetch pinned data."""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.request
import venv
import zipfile
import io

ROOT=Path(__file__).resolve().parent.parent
COMMIT='428bc4f57a307b651c1b02054ea68bcb092552b7'
BASE=f'https://raw.githubusercontent.com/GrupoDiana/BRTLibrary_Examples/{COMMIT}/example_1_basic_spatialisation_rtaudio/resources/'
FILES={
 'sadie-d2.sofa':('3DTI_HRTF_SADIE_II_D2_256s_44100Hz.sofa','53d4d0faa5688b24ce690e75fd93462e9afb02e8b0539e805b5e09fea51a2501'),
 'nearfield-48000.sofa':('NearFieldCompensation_ILD_48000.sofa','15c6a94098e5a29e06fa745d4b2a30df7cd0031a2b00c8cb0679b2d0b47e7c2c'),
 'speech.wav':('speech.wav','2c64983ae6c5d3f5fb6af892b152c137c161ba89e9beb91bde1309a08748aee8')}

def main():
    env=ROOT/'.runtime/spatial-env';data=ROOT/'.runtime/spatial-data';data.mkdir(parents=True,exist_ok=True)
    python=env/('Scripts/python.exe' if sys.platform=='win32' else 'bin/python')
    if not python.is_file():venv.EnvBuilder(with_pip=True).create(env)
    uv=ROOT/'.runtime/installer/bin'/('uv.exe' if sys.platform=='win32' else 'uv')
    if not uv.is_file():
        subprocess.run([str(python),'-m','ensurepip'],check=True,stdout=subprocess.DEVNULL)
    args=[str(uv),'pip','install','--python',str(python)] if uv.is_file() else [str(python),'-m','pip','install']
    subprocess.run([*args,'-r',str(ROOT/'requirements-spatial.txt')],check=True)
    for name,(source,digest) in FILES.items():
        target=data/name
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest()==digest:continue
        with urllib.request.urlopen(BASE+source,timeout=60) as response:raw=response.read(30_000_000)
        if hashlib.sha256(raw).hexdigest()!=digest:raise RuntimeError('Spatial resource checksum mismatch: '+name)
        temporary=target.with_suffix('.download');temporary.write_bytes(raw);temporary.replace(target)
    ku100=data/'ku100-150.sofa';ku100_hash='9bbd0213da99a3d4a48b1c3a04e1fee3f74fc099e683fc7949c06e9a54341730'
    if not ku100.is_file() or hashlib.sha256(ku100.read_bytes()).hexdigest()!=ku100_hash:
        with urllib.request.urlopen('https://audiogroup.web.th-koeln.de/FILES/NFHRIR_L2702_SOFA.zip',timeout=60) as response:archive=response.read(20_000_000)
        if hashlib.sha256(archive).hexdigest()!='5f3a4c6df0f0c0bbbce6639f41129a20bc03603d19316db74637f70bfa1ff5f9':raise RuntimeError('KU100 archive checksum mismatch.')
        raw=zipfile.ZipFile(io.BytesIO(archive)).read('HRIR_L2702_NF150.sofa')
        if hashlib.sha256(raw).hexdigest()!=ku100_hash:raise RuntimeError('KU100 response checksum mismatch.')
        ku100.write_bytes(raw)
    license_url=f'https://raw.githubusercontent.com/GrupoDiana/BRTLibrary_Examples/{COMMIT}/LICENSE'
    if not (data/'BRT-EXAMPLES-LICENSE.txt').exists():
        with urllib.request.urlopen(license_url,timeout=30) as response:(data/'BRT-EXAMPLES-LICENSE.txt').write_bytes(response.read())
    (data/'provenance.json').write_text(json.dumps({'source':BASE,'commit':COMMIT,'files':FILES,'ku100':{'source':'https://audiogroup.web.th-koeln.de/ku100nfhrir.html','sha256':ku100_hash,'attribution':'Johannes M. Arend, Annika Neidhardt and Christoph Poerschmann, TH Koeln; Neumann KU100 HRIR compilation. SOFA metadata CC BY-SA 3.0; download site CC BY-SA 4.0.'},
      'notes':'SADIE II D2 measured HRIR; BRT examples Duda-model near-field SOS. Dataset metadata and upstream license accompany resources. No BRT C++ code is incorporated into the renderer.'},indent=2))
    print('Desktop binaural rendering is installed. Restart the phone server when no visit is active.')

if __name__=='__main__':main()
