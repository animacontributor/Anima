"""Kyutai TTS 1.6B adapter using released expressive voice embeddings.

Models remain in the speech sidecar. Windows uses the PyTorch reference
implementation with torch.compile disabled; no system/WSL installation.
"""
from __future__ import annotations
import os
import re
import time
import subprocess
import tempfile
import shutil
import sys
from pathlib import Path
import delivery as delivery_options
import config

STYLES = {
    "calm": "expresso/ex04-ex02_calm_001_channel2_336s.wav",
    "desire": "expresso/ex04-ex02_desire_001_channel2_694s.wav",
    "happy": "expresso/ex04-ex02_happy_001_channel2_140s.wav",
    "neutral": "expresso/ex01-ex02_default_001_channel2_198s.wav",
    "whisper": "expresso/ex01-ex02_whisper_001_channel2_717s.wav",
}

class AudioQualityError(RuntimeError):
    pass

def validate_audio(pcm):
    import numpy as np
    if not len(pcm) or not np.isfinite(pcm).all():raise AudioQualityError('Invalid/non-finite decoded audio.')
    if np.max(np.abs(pcm))>4:raise AudioQualityError('Decoded audio amplitude is invalid.')
    if np.mean(np.abs(pcm)>=.99)>.2 and np.sqrt(np.mean(pcm*pcm))>.7:raise AudioQualityError('Sustained severely clipped audio.')

def stretch(pcm,rate,speed,cancel=None):
    if abs(speed-1)<1e-8:return pcm,rate
    import soundfile as sf
    import soxr
    root=Path(__file__).resolve().parent.parent
    private=root/'.runtime/rubberband/rubberband-4.0.0-gpl-executable-windows/rubberband-r3.exe'
    binary=str(private) if private.is_file() else shutil.which('rubberband-r3') or shutil.which('rubberband')
    if not binary:raise RuntimeError('Pitch-preserving delivery is not installed. Run engine/setup_delivery.py on Windows, install Rubber Band R3 on other platforms, or use speed 100%.')
    home=root/'.runtime/voice-tempo';home.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='phrase-',dir=home) as folder:
        target=Path(folder).resolve()
        if not target.is_relative_to(home.resolve()):raise RuntimeError('Voice temporary path escaped runtime.')
        audio=soxr.resample(pcm,rate,48000,quality='VHQ') if rate!=48000 else pcm
        src=target/'source.wav';dst=target/'stretched.wav';sf.write(src,audio,48000,subtype='FLOAT')
        process=subprocess.Popen([str(binary),'-3','-q','-T',str(speed),str(src),str(dst)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            deadline=time.monotonic()+45
            while process.poll() is None:
                if cancel is not None and cancel.is_set():raise InterruptedError('Voice generation cancelled.')
                if time.monotonic()>deadline:raise RuntimeError('Voice tempo processing timed out.')
                time.sleep(.02)
            if process.returncode:raise RuntimeError('Pitch-preserving tempo processing failed.')
            out,out_rate=sf.read(dst,dtype='float32');validate_audio(out);return out,out_rate
        finally:
            if process.poll() is None:process.kill();process.wait(timeout=3)
            if process.stderr:process.stderr.close()


class KyutaiVoice:
    def __init__(self, device="cuda"):
        os.environ.setdefault("NO_TORCH_COMPILE", "1")
        from moshi.models.loaders import CheckpointInfo
        from moshi.models.tts import TTSModel
        checkpoint = CheckpointInfo.from_hf_repo("kyutai/tts-1.6b-en_fr")
        self.model = TTSModel.from_checkpoint_info(checkpoint, n_q=32, temp=.6, device=device)
        self.sample_rate = int(self.model.mimi.sample_rate)
        self.conditions = {}

    def condition(self,selected,mix,guidance):
        from moshi.conditioners import TensorCondition
        key=(selected,round(mix,3),guidance)
        if key in self.conditions:return self.conditions[key]
        def load(name):return self.model.make_condition_attributes([self.model.get_voice_path(name)],cfg_coef=guidance)
        base=load(selected)
        if mix:
            whisper=load(STYLES['whisper'])
            if mix==1:base=whisper
            else:
                left=base.tensor['speaker_wavs'];right=whisper.tensor['speaker_wavs']
                if left.tensor.shape!=right.tensor.shape:raise ValueError('Voice references cannot be blended: conditioning dimensions differ.')
                import torch
                if not torch.equal(left.mask,right.mask):raise ValueError('Voice reference masks differ.')
                base=base.copy();base.tensor['speaker_wavs']=TensorCondition((1-mix)*left.tensor+mix*right.tensor,left.mask.clone())
        self.conditions[key]=base
        if len(self.conditions)>32:self.conditions.pop(next(iter(self.conditions)))
        return base

    def synthesize(self, text, style="calm", voice=None, on_audio=None, delivery=None, cancel=None):
        import numpy as np
        import torch
        if style not in STYLES:
            raise ValueError("Kyutai style must be calm, desire, happy, neutral or whisper.")
        selected = voice or STYLES[style]
        controls=delivery_options.clean(delivery)
        condition=self.condition(selected,controls['whisper_mix'],controls['guidance'])
        text = re.sub(r"\[(?:laugh|chuckle|sigh|gasp|cough|clear throat|sniff)\]", "", text).strip()
        split_replies=bool(getattr(config,'LIVE_VOICE_SPLIT_REPLIES',False))
        sections=delivery_options.sections(text) if split_replies else ([text] if text else [])
        duration_limit=35 if split_replies else max(35,float(getattr(config,'LIVE_VOICE_TIMEOUT_S',300)))
        pieces = []
        started = time.monotonic()
        first_audio = None
        output_rate=self.sample_rate if controls['speed']==1 else 48000
        progressive=on_audio is not None and controls['speed']==1
        def emit(samples):
            nonlocal first_audio
            if cancel is not None and cancel.is_set():raise InterruptedError('Voice generation cancelled.')
            if first_audio is None:first_audio=time.monotonic()-started
            if on_audio is None:pieces.append(samples)
            else:
                for at in range(0,len(samples),int(output_rate*.48)):
                    if cancel is not None and cancel.is_set():raise InterruptedError('Voice generation cancelled.')
                    on_audio(samples[at:at+int(output_rate*.48)],output_rate)
        for index,section in enumerate(sections):
            if cancel is not None and cancel.is_set():raise InterruptedError('Voice generation cancelled.')
            # Live audio keeps one generation context, publishing checked PCM
            # as it arrives. Tempo-adjusted/non-streaming audio stays buffered.
            pcm=None
            section_emitted=False
            for attempt in range(2):
                frames=[];same=0;previous=None;sample_count=0;pending_count=0
                def frame_ready(frame):
                    nonlocal same,previous,sample_count,pending_count,section_emitted
                    if cancel is not None and cancel.is_set():raise InterruptedError('Voice generation cancelled.')
                    if (frame != -1).all():
                        samples=self.model.mimi.decode(frame[:,1:]).detach().cpu().numpy()[0,0]
                        validate_audio(samples);sample_count+=len(samples)
                        if sample_count>self.sample_rate*duration_limit:raise AudioQualityError('Speech generation exceeded its duration limit.')
                        signature=frame[:,1:].detach().cpu().numpy().tobytes()
                        same=same+1 if signature==previous else 0;previous=signature
                        if same>=8 and np.sqrt(np.mean(samples*samples))>.01:raise AudioQualityError('Repeated non-silent audio-code frames.')
                        frames.append(samples)
                        if progressive:
                            pending_count+=len(samples)
                            # Start with 160 ms; subsequent packets can be larger.
                            target=int(self.sample_rate*(.16 if first_audio is None else .48))
                            if pending_count>=target:
                                packet=np.clip(np.concatenate(frames),-1,1)
                                if first_audio is None:
                                    fade=min(int(self.sample_rate*.005),len(packet)//2)
                                    if fade:packet[:fade]*=np.linspace(0,1,fade)
                                emit(packet);section_emitted=True;frames.clear();pending_count=0
                try:
                    entries=self.model.prepare_script([section],padding_between=1)
                    with torch.inference_mode(),self.model.mimi.streaming(1):
                        self.model.generate([entries],[condition],on_frame=frame_ready)
                    if not sample_count:raise AudioQualityError('No audio for a speech section.')
                    if progressive:
                        if frames:emit(np.clip(np.concatenate(frames),-1,1));section_emitted=True
                        break
                    pcm=np.concatenate(frames);validate_audio(pcm);break
                except AudioQualityError as e:
                    print(f'Kyutai: section {index+1}/{len(sections)}, attempt {attempt+1}: {e}',file=sys.stderr,flush=True)
                    # Replaying an already audible prefix would duplicate words.
                    if attempt or section_emitted:raise
            if progressive:
                if index+1<len(sections) and controls['pause_ms']:emit(np.zeros(int(output_rate*controls['pause_ms']/1000),dtype=np.float32))
                continue
            pcm=np.clip(pcm,-1,1)
            pcm,rate=stretch(pcm,self.sample_rate,controls['speed'],cancel)
            if rate!=output_rate:raise RuntimeError('Delivery rate changed unexpectedly.')
            fade=min(int(rate*.005),len(pcm)//2)
            if fade:pcm[:fade]*=np.linspace(0,1,fade);pcm[-fade:]*=np.linspace(1,0,fade)
            if index and controls['pause_ms']:emit(np.zeros(int(rate*controls['pause_ms']/1000),dtype=np.float32))
            emit(pcm)
        if on_audio is not None:
            if first_audio is None:raise RuntimeError('Kyutai produced no speech.')
            return None,output_rate,first_audio
        if not pieces:
            raise RuntimeError("Kyutai produced no speech.")
        return np.concatenate(pieces, axis=-1), output_rate, first_audio
