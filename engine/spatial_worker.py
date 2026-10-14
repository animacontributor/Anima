"""CPU binaural rendering sidecar. No models, network, microphone or memory access.

Measured, time-aligned SADIE HRIRs + Duda near-field SOS resources from BRT's
examples. The convolution implementation is independent; this is not the BRT
C++ engine. SOFA resources and their provenance remain outside the APK.
"""
from __future__ import annotations
import base64
from fractions import Fraction
import io
import json
from pathlib import Path
import sys
import time
import wave

import h5py
import numpy as np
from scipy import signal
from scipy.spatial import cKDTree

RATE = 48000
DATA = Path(__file__).resolve().parent.parent / '.runtime' / 'spatial-data'

def clean(value):
    def number(key, default, low, high):
        x=float(value.get(key,default))
        if not np.isfinite(x):raise ValueError('Spatial settings must be finite.')
        return max(low,min(high,x))
    profile=value.get('profile','ku100')
    if profile not in ('ku100','sadie'):raise ValueError('Unknown binaural profile.')
    return {'enabled':bool(value.get('enabled',False)), 'profile':profile,'azimuth':number('azimuth',0,-180,180),
            'elevation':number('elevation',0,-60,60),'distance':number('distance',.2,.11,3),
            'room':number('room',.06,0,.2),'head_radius':number('head_radius',.09,.075,.095)}

def point(settings):
    # SOFA: +X front, +Y left, +Z up. Phone: positive azimuth is right.
    a,e=np.deg2rad([settings['azimuth'],settings['elevation']]);d=settings['distance']
    return np.array([np.cos(a)*np.cos(e),-np.sin(a)*np.cos(e),np.sin(e)])*d

def delayed(ir, delay):
    """Fractional delay with a short windowed-sinc FIR; retain phase/ear delay."""
    integer=max(0,int(np.floor(delay)));fraction=delay-integer
    if abs(fraction)<1e-8:return np.pad(ir,(integer,0))
    taps=np.arange(17)-8;kernel=np.sinc(taps-fraction)*np.kaiser(17,8.6);kernel/=kernel.sum()
    # Every ear gets the same extra 8 samples, including integer-delay cases.
    return np.pad(signal.fftconvolve(ir,kernel),(integer,0))

class Responses:
    def __init__(self,profile='ku100'):
        self.profile=profile
        with h5py.File(DATA/('ku100-150.sofa' if profile=='ku100' else 'sadie-d2.sofa'),'r') as f:
            fs=float(f['Data.SamplingRate'][0]);positions=f['SourcePosition'][:]
            # KU100's converted receiver coordinates disagree with its measured
            # channel levels at cardinal azimuths. The pinned IR data is L,R;
            # verify this with the cardinal-direction regression, not metadata alone.
            order=np.array([0,1]) if profile=='ku100' else np.argsort(-f['ReceiverPosition'][:,1,0])
            self.measured_head_radius=float(np.mean(np.linalg.norm(f['ReceiverPosition'][:,:,0],axis=1)))
            self.ir=f['Data.IR'][:][:,order,:]
            if fs==44100:self.ir=signal.resample_poly(self.ir,160,147,axis=-1)*(147/160)
            if fs not in (44100,48000):raise ValueError('Unsupported reference HRIR rate.')
            delay=f['Data.Delay'][:]
            self.delay=np.broadcast_to(delay,(len(positions),2))[:,order].copy()*RATE/fs
            if profile=='ku100':
                threshold=np.max(np.abs(self.ir),axis=-1)*.003
                onset=np.argmax(np.abs(self.ir)>threshold[:,:,None],axis=-1)
                anchor=int(np.max(onset));aligned=np.zeros((len(positions),2,self.ir.shape[2]+anchor))
                for m in range(len(positions)):
                    for ear in range(2):
                        shift=anchor-onset[m,ear];aligned[m,ear,shift:shift+self.ir.shape[2]]=self.ir[m,ear]
                self.ir=aligned;self.delay+=onset-anchor
        a,e=np.deg2rad(positions[:,:2]).T
        self.directions=np.stack([np.cos(a)*np.cos(e),np.sin(a)*np.cos(e),np.sin(e)],axis=1)
        self.tree=cKDTree(self.directions);self.radius=float(np.median(positions[:,2]))
        with h5py.File(DATA/'nearfield-48000.sofa','r') as f:
            if float(f['Data.SamplingRate'][0])!=RATE:raise ValueError('Near-field rate mismatch.')
            p=f['SourcePosition'][:];self.angles=np.unique(p[:,0]);self.distances=np.unique(p[:,2])
            self.sos=f['Data.SOS'][:,0,:].reshape(len(self.distances),len(self.angles),2,6)
        self.nfc_cache={};self.cache={}

    def hrir(self, direction, ear):
        direction=direction/np.linalg.norm(direction);dist,idx=self.tree.query(direction,k=3)
        if dist[0]<1e-8:weights=np.array([1.,0.,0.])
        else:weights=1/np.maximum(dist,1e-8)**2;weights/=weights.sum()
        # The supplied SADIE resource separates arrival delays from aligned FIRs.
        return np.sum(self.ir[idx,ear,:]*weights[:,None],axis=0),float(self.delay[idx,ear]@weights)

    def nearfield(self, angle, distance):
        # Interpolate stable impulse responses, not denominator coefficients.
        def bounds(grid,x):
            x=np.clip(x,grid[0],grid[-1]);hi=min(len(grid)-1,int(np.searchsorted(grid,x)));lo=max(0,hi-1)
            alpha=0. if grid[hi]==grid[lo] else float((x-grid[lo])/(grid[hi]-grid[lo]))
            return [(lo,1-alpha),(hi,alpha)]
        result=np.zeros(2048);impulse=np.zeros(2048);impulse[0]=1
        for di,dw in bounds(self.distances,distance):
            for ai,aw in bounds(self.angles,angle):
                if dw*aw==0:continue
                key=(di,ai)
                if key not in self.nfc_cache:self.nfc_cache[key]=signal.sosfilt(self.sos[di,ai],impulse)
                result+=dw*aw*self.nfc_cache[key]
        return result

    def direct(self, source, head_radius, near=True):
        distance=float(np.linalg.norm(source));responses=[];delays=[]
        interaural=float(np.rad2deg(np.arcsin(np.clip(-source[1]/distance,-1,1))))
        for ear,y in enumerate([head_radius,-head_radius]):
            origin=np.array([0,y,0]);ray=source-origin;ray/=np.linalg.norm(ray)
            # Ray from this ear intersects the measurement sphere: acoustic parallax.
            dot=float(origin@ray);travel=-dot+np.sqrt(dot*dot+self.radius**2-origin@origin)
            projected=origin+travel*ray;hrir,delay=self.hrir(projected,ear)
            if near and distance<2:
                model_scale=.09/head_radius
                correction=self.nearfield(interaural if ear==0 else -interaural,distance*model_scale)
                # Remove the resource's finite-distance baseline to avoid double gain.
                baseline=self.nearfield(interaural if ear==0 else -interaural,self.radius*model_scale)
                n=4096;ratio=np.fft.rfft(correction,n)/np.maximum(np.abs(np.fft.rfft(baseline,n)),1e-8)*np.exp(-1j*np.angle(np.fft.rfft(baseline,n)))
                correction=np.fft.irfft(ratio,n)[:2048]
                hrir=signal.fftconvolve(hrir,correction)
            responses.append(hrir);delays.append(delay)
        common=min(delays)
        out=[]
        for h,d in zip(responses,delays):
            # Add the common interpolation latency for all paths, not just fractions.
            delay=(d-common)*head_radius/self.measured_head_radius
            h=delayed(h,delay);out.append(h if abs(delay%1)>1e-8 else np.pad(h,(8,0)))
        length=max(map(len,out));return np.stack([np.pad(h,(0,length-len(h))) for h in out])

    def prepare(self, settings):
        s=clean(settings);key=json.dumps(s,sort_keys=True)
        if key in self.cache:return self.cache[key]
        started=time.perf_counter();source=point(s);direct=self.direct(source,s['head_radius']);distance=np.linalg.norm(source)
        # One scalar preserves the ear-to-ear level, phase and spectral relationship.
        norm=np.sqrt(np.sum(direct*direct));direct*=.85/max(norm,1e-8)
        length=max(direct.shape[1],int(RATE*.28) if s['room'] else direct.shape[1])
        kernel=np.pad(direct,((0,0),(0,length-direct.shape[1])))
        if s['room']:
            # Image-source first reflections in a small damped room, then a quiet
            # decorrelated decay. The close-ear direct/room ratio stays dominant.
            walls=[[-max(2.2,abs(source[0])+.6),max(2.2,abs(source[0])+.6)],[-max(2.6,abs(source[1])+.6),max(2.6,abs(source[1])+.6)],[-max(1.2,-source[2]+.5),max(1.4,source[2]+.5)]]
            for axis in range(3):
                for wall in walls[axis]:
                    image=source.copy();image[axis]=2*wall-source[axis];path=np.linalg.norm(image)
                    response=self.direct(image,s['head_radius'],near=False)
                    delay=int(round((path-distance)/343*RATE));gain=s['room']*.65*min(distance,.8)/path
                    count=min(response.shape[1],length-delay)
                    if count>0:kernel[:,delay:delay+count]+=gain*response[:,:count]
            rng=np.random.default_rng(42);t=np.arange(length)/RATE
            tail=rng.normal(size=(2,length))*np.exp(-6.9078*t/.18)
            tail[:,:int(RATE*.018)]=0
            tail=signal.sosfilt(signal.butter(2,[250,6500],btype='bandpass',fs=RATE,output='sos'),tail,axis=-1)
            tail*=s['room']*.12*min(distance/.5,1)/max(np.sqrt(np.sum(tail*tail)),1e-9)
            kernel+=tail
        nearest=min(np.linalg.norm(source-np.array([0,s['head_radius'],0])),np.linalg.norm(source-np.array([0,-s['head_radius'],0])))
        info={'engine':'desktop measured HRIR / near-field convolution','profile':'Neumann KU100' if self.profile=='ku100' else 'SADIE II D2','rate':RATE,'channels':2,'nearest_ear_cm':round(float(nearest*100),1),'kernel_ms':round(length/RATE*1000,1),'prepare_ms':round((time.perf_counter()-started)*1000,1),'settings':s}
        self.cache[key]=(kernel,info)
        if len(self.cache)>12:self.cache.pop(next(iter(self.cache)))
        return kernel,info

class Stream:
    def __init__(self,kernel):self.kernel=kernel;self.tail=np.zeros((2,kernel.shape[1]-1));self.rate=None;self.zi=None;self.processed=0;self.output_count=0;self.history=np.zeros(0);self.gain=1.;self.limited=0
    def resample(self,x,rate):
        if self.rate is None:
            self.rate=rate;ratio=Fraction(RATE,rate);self.up,self.down=ratio.numerator,ratio.denominator
            self.resample_filter=signal.firwin(48*max(self.up,self.down)+1,1/max(self.up,self.down),window=('kaiser',10.)) * self.up if rate!=RATE else np.ones(1)
            self.zi=np.zeros(len(self.resample_filter)-1)
        if rate!=self.rate:raise ValueError('Speech rate changed mid-stream.')
        # Polyphase evaluation avoids huge zero-expanded buffers for 44.1 -> 48 kHz.
        # Align each history window to the global rational resampling phase.
        keep=int(np.ceil((len(self.resample_filter)-1)/self.up/self.down))*self.down
        start=max(0,((self.processed-keep)//self.down)*self.down)
        history_count=self.processed-start
        joined=np.concatenate([self.history[-history_count:] if history_count else np.zeros(0),x])
        filtered=signal.upfirdn(self.resample_filter,joined,up=self.up,down=self.down)
        base=start*self.up//self.down;end=int(np.ceil((self.processed+len(x))*self.up/self.down))
        out=filtered[self.output_count-base:end-base];self.output_count=end;self.processed+=len(x)
        self.history=joined[-(keep+self.down):].copy()
        return out
    def process(self,x,rate):
        x=self.resample(x,rate);out=np.stack([signal.fftconvolve(x,h) for h in self.kernel])
        out[:,:self.tail.shape[1]]+=self.tail
        self.tail=out[:,len(x):].copy();return self.limit(out[:,:len(x)])
    def limit(self,out):
        # Stereo-linked protection only engages for a peak that would clip PCM.
        release=1-np.exp(-1/(RATE*.08));maximum=np.max(np.abs(out),axis=0)
        for i,p in enumerate(maximum):
            target=min(1.,.97/max(p,1e-9));self.gain=min(target,self.gain+(1-self.gain)*release)
            if self.gain<.999:self.limited+=1
            out[:,i]*=self.gain
        return out
    def finish(self):
        # Flush the interpolation FIR before draining the acoustic response.
        if self.rate is not None and len(self.zi):
            lead=self.process(np.zeros(int(np.ceil(len(self.zi)/self.up))),self.rate)
        else:lead=np.zeros((2,0))
        return np.concatenate([lead,self.limit(self.tail.copy())],axis=1)

def wav(samples):
    # Stereo 24-bit PCM: no lossy compression or per-ear normalization.
    pcm=np.rint(np.clip(samples.T,-1,1)*8388607).astype(np.int32).reshape(-1)
    raw=np.empty((len(pcm),3),dtype=np.uint8)
    for i in range(3):raw[:,i]=(pcm>>(8*i))&255
    out=io.BytesIO()
    with wave.open(out,'wb') as f:f.setnchannels(2);f.setsampwidth(3);f.setframerate(RATE);f.writeframes(raw.tobytes())
    return out.getvalue()

def readwav(raw):
    with wave.open(io.BytesIO(raw),'rb') as f:
        width=f.getsampwidth()
        if f.getnchannels()!=1 or width not in (2,3) or f.getnframes()>48000*35:raise ValueError('Expected bounded mono PCM speech.')
        raw=f.readframes(f.getnframes())
        if width==2:samples=np.frombuffer(raw,dtype='<i2').astype(np.float64)/32768
        else:
            b=np.frombuffer(raw,dtype=np.uint8).reshape(-1,3).astype(np.int32);values=b[:,0]|(b[:,1]<<8)|(b[:,2]<<16);values=(values^0x800000)-0x800000;samples=values.astype(np.float64)/8388608
        return samples,f.getframerate()

def main():
    profiles={};streams={}
    for line in sys.stdin:
        try:
            request=json.loads(line);op=request['op']
            profile=clean(request.get('settings',{}))['profile']
            if op in ('prepare','begin','preview'):
                if profile not in profiles:profiles[profile]=Responses(profile)
                responses=profiles[profile]
            if op=='prepare':_,info=responses.prepare(request['settings']);result=info
            elif op=='begin':kernel,info=responses.prepare(request['settings']);streams[request['stream']]=Stream(kernel);result=info
            elif op=='process':
                stream=streams[request['stream']];samples,rate=readwav(base64.b64decode(request['audio'],validate=True));out=stream.process(samples,rate);result={'audio':base64.b64encode(wav(out)).decode(),'seconds':out.shape[1]/RATE}
            elif op=='finish':
                stream=streams.pop(request['stream']);out=stream.finish();result={'audio':base64.b64encode(wav(out)).decode(),'seconds':out.shape[1]/RATE,'limited_samples':stream.limited}
            elif op=='close':streams.pop(request['stream'],None);result={'ok':True}
            elif op=='preview':
                kernel,info=responses.prepare(request['settings']);stream=Stream(kernel)
                preview=DATA/'voice-preview.wav'
                if preview.is_file():source,rate=readwav(preview.read_bytes());source=source[:rate*12]
                else:
                    with wave.open(str(DATA/'speech.wav'),'rb') as f:
                        frames=f.readframes(min(f.getnframes(),int(f.getframerate()*8)));source=np.frombuffer(frames,dtype='<i2').reshape(-1,f.getnchannels()).mean(axis=1)/32768;rate=f.getframerate()
                out=np.concatenate([stream.process(source,rate),stream.finish()],axis=1);result={'audio':base64.b64encode(wav(out)).decode(),'seconds':out.shape[1]/RATE,**info}
            else:raise ValueError('Unknown spatial operation.')
            print(json.dumps({'ok':True,**result}),flush=True)
        except Exception as e:print(json.dumps({'error':str(e)}),flush=True)

if __name__=='__main__':main()
