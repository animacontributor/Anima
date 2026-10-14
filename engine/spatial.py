"""Stdlib bridge to the optional CPU stereo rendering sidecar."""
from __future__ import annotations
import atexit
import base64
import json
import math
from pathlib import Path
import subprocess
import sys
import threading
import uuid
import config

DEFAULTS={'enabled':False,'profile':'ku100','azimuth':0.,'elevation':0.,'distance':.2,'room':.06,'head_radius':.09}

def settings(value):
    if not isinstance(value,dict):raise ValueError('Spatial settings must be an object.')
    profile=value.get('profile','ku100')
    if profile not in ('ku100','sadie'):raise ValueError('Unknown binaural profile.')
    out={'enabled':bool(value.get('enabled',False)),'profile':profile}
    for key,low,high in [('azimuth',-180,180),('elevation',-60,60),('distance',.11,3),('room',0,.2),('head_radius',.075,.095)]:
        number=float(value.get(key,DEFAULTS[key]))
        if not math.isfinite(number):raise ValueError('Spatial settings must be finite.')
        out[key]=max(low,min(high,number))
    return out

def voice_position(value):
    """Ear distances in the same head-centred geometry as the binaural renderer."""
    s=settings(value)
    if not s['enabled']:return {'enabled':False,'close':False}
    a,e=math.radians(s['azimuth']),math.radians(s['elevation'])
    x=s['distance']*math.cos(a)*math.cos(e)
    y=-s['distance']*math.sin(a)*math.cos(e)
    z=s['distance']*math.sin(e)
    left=math.sqrt(x*x+(y-s['head_radius'])**2+z*z)
    right=math.sqrt(x*x+(y+s['head_radius'])**2+z*z)
    distance=min(left,right)
    return {'enabled':True,'ear':'left' if left<=right else 'right',
            'distance_cm':round(distance*100,1),'close':distance<.1-1e-9}

class Worker:
    def __init__(self):self.lock=threading.RLock();self.process=None;self.root=config.ROOT
    def installed(self):return self.python().is_file() and all((self.root/'.runtime/spatial-data'/name).is_file() for name in ['sadie-d2.sofa','ku100-150.sofa','nearfield-48000.sofa','speech.wav'])
    def python(self):return self.root/'.runtime/spatial-env'/('Scripts/python.exe' if sys.platform=='win32' else 'bin/python')
    def request(self,op,**data):
        with self.lock:
            if not self.process or self.process.poll() is not None:
                if not self.installed():raise ValueError('Desktop 3D rendering is not installed. Run engine/setup_spatial.py on the computer.')
                self.process=subprocess.Popen([str(self.python()),'-u',str(self.root/'engine/spatial_worker.py')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8',cwd=self.root,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.process.stdin.write(json.dumps({'op':op,**data})+'\n');self.process.stdin.flush()
            line=self.process.stdout.readline()
            if not line:self.close();raise ValueError('The desktop 3D renderer stopped. Try applying the position again.')
            result=json.loads(line)
            if result.get('error'):raise ValueError(result['error'])
            return result
    def close(self):
        with self.lock:
            process,self.process=self.process,None
            if process:
                if process.poll() is None:process.terminate()
                try:process.wait(timeout=3)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
                for pipe in (process.stdin,process.stdout):
                    if pipe:pipe.close()

WORKER=Worker();atexit.register(WORKER.close)

class Render:
    def __init__(self,params,worker=WORKER):
        self.worker=worker;self.id=uuid.uuid4().hex;self.closed=False
        self.info=worker.request('begin',stream=self.id,settings=settings(params))
    def process(self,raw):
        result=self.worker.request('process',stream=self.id,audio=base64.b64encode(raw).decode())
        return base64.b64decode(result['audio'],validate=True),result['seconds']
    def finish(self):
        if self.closed:return None
        self.closed=True;result=self.worker.request('finish',stream=self.id)
        return base64.b64decode(result['audio'],validate=True),result['seconds']
    def close(self):
        if not self.closed:
            self.closed=True
            try:self.worker.request('close',stream=self.id)
            except Exception:pass
