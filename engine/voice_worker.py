"""Persistent speech sidecar. JSON lines in/out; model diagnostics go to stderr.

Only this interpreter imports speech dependencies. No identity, journal or
memory is read or written here. Models are downloaded to .runtime/models.
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import sys
import queue
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("HF_HOME", str(ROOT / ".runtime" / "models"))
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
_tts = None
_stt = None
_stt_key = None
_stt_parked = False
_stt_warmed = False
_reference = ""
_warmed = False
_clone_prompt = None


class SpeechCancelled(Exception):
    pass


def cuda_memory_stats() -> dict:
    state = {"pid": os.getpid(), "recognizer_parked": _stt_parked,
             "recognizer_device": _stt_key[1] if _stt_key else None}
    torch = sys.modules.get("torch")
    if torch is None or not torch.cuda.is_initialized():
        return dict(state, allocated_bytes=0, reserved_bytes=0)
    return {**state, "allocated_bytes": int(torch.cuda.memory_allocated()),
            "reserved_bytes": int(torch.cuda.memory_reserved())}


def release_cuda_cache() -> None:
    """Free unused allocator/graph storage; keep the live speech models loaded."""
    torch = sys.modules.get("torch")
    if torch is not None and torch.cuda.is_initialized():
        import gc
        gc.collect()
        try:
            torch.cuda.empty_cache()
        except RuntimeError as e:
            # Cache cleanup must not replace valid speech or the original error.
            print(f"Unused CUDA cache could not be released: {e}", file=sys.stderr)


def encode_audio(samples, rate, subtype="PCM_16"):
    import soundfile as sf
    buf = io.BytesIO()
    if subtype not in ('PCM_16','PCM_24'):raise ValueError('Unsupported speech PCM format.')
    sf.write(buf, samples, rate, format="WAV", subtype=subtype)
    return {"audio": base64.b64encode(buf.getvalue()).decode("ascii"), "seconds": len(samples) / rate}


def check(req) -> dict:
    import importlib.util
    package = {"qwen": "qwen_tts", "kyutai": "moshi"}.get(req.get("backend"), "chatterbox")
    missing = [p for p in (package, "faster_whisper", "soundfile", "torch")
               if importlib.util.find_spec(p) is None]
    return {"installed": not missing, "missing": missing, "python": sys.version.split()[0]}



def park_recognizer(req: dict) -> None:
    """Keep CUDA recognition weights in CPU storage while the brain/voice runs.

    Recognition still runs on its original device and precision after reload.
    CTranslate2 owns this allocation; torch.empty_cache cannot release it.
    """
    global _stt_parked
    native = getattr(_stt, "model", None)
    if (req.get("park_stt", True) and _stt_key and _stt_key[1] == "cuda"
            and native is not None and not _stt_parked):
        native.unload_model(to_cpu=True)
        _stt_parked = True


def resume_recognizer() -> None:
    global _stt_parked
    if _stt_parked:
        _stt.model.load_model()
        _stt_parked = False


def models(req: dict, speech=True, require_voice=True):
    global _tts, _stt, _stt_key, _stt_warmed, _stt_parked, _reference, _clone_prompt
    # Offline is set BEFORE importing Hugging Face/transformers.
    if req.get("offline"):
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
    if req.get("backend") == "kyutai":
        os.environ.setdefault("NO_TORCH_COMPILE", "1")
    name = req.get("stt_model") or "small.en"
    stt_device = req.get("stt_device") or "cpu"
    if stt_device not in ("cpu", "cuda"):
        raise ValueError("Speech recognition device must be cpu or cuda.")
    compute_type = "float16" if stt_device == "cuda" else "int8"
    stt_key = (name, stt_device, compute_type)
    if _stt is None or stt_key != _stt_key:
        from faster_whisper import WhisperModel
        _stt = WhisperModel(name, device=stt_device, compute_type=compute_type,
                            download_root=str(ROOT / ".runtime" / "models" / "whisper"),
                            local_files_only=bool(req.get("offline")))
        _stt_key = stt_key
        _stt_warmed = False
        _stt_parked = False
    if speech:
        park_recognizer(req)
    else:
        resume_recognizer()
    if speech and req.get("backend") == "kyutai":
        if _tts is None:
            from kyutai_voice import KyutaiVoice
            _tts = KyutaiVoice(req.get("device") or "cuda")
        return
    if speech and req.get("backend") == "qwen":
        if _tts is None:
            import torch
            from qwen_tts import Qwen3TTSModel
            where = req.get("device") or "cuda"
            if where == "cuda" and not torch.cuda.is_available():
                raise RuntimeError("CUDA is unavailable in the Qwen voice environment.")
            _tts = Qwen3TTSModel.from_pretrained("Qwen/Qwen3-TTS-12Hz-1.7B-Base",
                        device_map="cuda:0" if where == "cuda" else where,
                        dtype=torch.bfloat16 if where == "cuda" else torch.float32, attn_implementation="sdpa")
        ref = req.get("reference") or ""
        if not ref:
            if require_voice:
                raise RuntimeError("Choose a voice sample or audition for Qwen first.")
            return
        if ref != _reference:
            import librosa
            audio, rate = librosa.load(ref, sr=16000, mono=True, duration=16)
            transcript = req.get("reference_text") or ""
            try:
                listing = json.loads((Path(ref).parent / "manifest.json").read_text(encoding="utf-8"))
                item = next(v for v in listing["voices"] if v["reference"] == Path(ref).name)
                transcript = transcript or item.get("reference_text") or listing.get("reference_text") or ""
            except (OSError, ValueError, KeyError, StopIteration, TypeError):
                pass
            if not transcript:
                resume_recognizer()
                segments, _ = _stt.transcribe(audio, language=req.get("language") or "en", beam_size=1)
                transcript = " ".join(s.text.strip() for s in segments).strip()
                park_recognizer(req)
            if not transcript:
                raise RuntimeError("No words were found in this voice sample.")
            _clone_prompt = _tts.create_voice_clone_prompt(ref_audio=(audio, rate), ref_text=transcript, x_vector_only_mode=False)
            _reference = ref
        return
    if speech and _tts is None:
        import torch
        from chatterbox.tts_turbo import ChatterboxTurboTTS
        from huggingface_hub import snapshot_download
        device = req.get("device") or "cuda"
        if device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA is unavailable in the voice Python. Run bat/setup-voice.bat, or choose LIVE_VOICE_DEVICE = 'cpu'.")
        # 0.1.6's from_pretrained unconditionally requests an HF token even
        # though these weights are public. Download explicitly without a login.
        local = snapshot_download("ResembleAI/chatterbox-turbo", token=False,
                                  allow_patterns=["*.safetensors", "*.json", "*.txt", "*.pt", "*.model"],
                                  local_files_only=bool(req.get("offline")))
        _tts = ChatterboxTurboTTS.from_local(local, device=device)
    ref = req.get("reference") or ""
    if speech and ref and ref != _reference:
        if not Path(ref).is_file():
            raise RuntimeError("The selected voice sample does not exist. Choose it again in the parlor.")
        _tts.prepare_conditionals(ref)
        _reference = ref
    if speech and require_voice and getattr(_tts, "conds", None) is None:
        raise RuntimeError("Choose a clean English voice sample longer than five seconds in the parlor before speaking.")


def handle(req: dict, emit=None, cancel=None) -> dict:
    if req.get("op") == "memory_stats":
        return cuda_memory_stats()
    if req.get("op") in {"warmup", "transcribe", "synthesize", "synthesize_stream", "download"}:
        try:
            return _handle(req, emit=emit, cancel=cancel)
        finally:
            try:
                park_recognizer(req)
            except RuntimeError as error:
                # Residency cleanup must not replace a valid transcript or error.
                print(f"Recognizer GPU parking failed: {error}", file=sys.stderr)
            release_cuda_cache()
    return _handle(req, emit=emit, cancel=cancel)


def _handle(req: dict, emit=None, cancel=None) -> dict:
    global _warmed, _stt_warmed
    if req["op"] == "check":
        return check(req)
    if req["op"] == "reference_info":
        import librosa
        samples, rate = librosa.load(req["path"], sr=24000, mono=True, duration=16)
        seconds = len(samples) / rate
        if seconds <= 5:
            raise ValueError("Choose a voice sample longer than five seconds; 6–15 seconds of clean English speech works best.")
        return {"seconds": seconds}
    if req["op"] == "warmup":
        models(req, speech=False)
        if not _stt_warmed:
            import numpy as np
            # Initialize recognition kernels before microphone capture starts.
            # The synthetic silence and any decoded text are discarded.
            segments, _ = _stt.transcribe(np.zeros(16000, dtype=np.float32),
                                         language=req.get("language") or None,
                                         vad_filter=False, beam_size=int(req.get("stt_beam_size") or 1),
                                         max_new_tokens=1, condition_on_previous_text=False)
            list(segments)
            _stt_warmed = True
        park_recognizer(req)
        if req.get("recognition_only"):
            return {"ready": True}
        models(req)
        if not _warmed:
            # Pay first CUDA/kernel and watermark initialization before
            # listening, instead of adding it to the first spoken reply.
            generate(req, "Hello.")
            _warmed = True
        return {"ready": True}
    if req["op"] == "download":
        models(req, require_voice=False)
        required = False if req.get("backend") == "kyutai" else _clone_prompt is None if req.get("backend") == "qwen" else getattr(_tts, "conds", None) is None
        return {"downloaded": True, "reference_required": required}
    if req["op"] == "transcribe":
        import soundfile as sf
        import numpy as np
        raw = base64.b64decode(req["audio"], validate=True)
        samples, rate = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
        if req.get("discord_input"):
            if rate != 48000 or samples.shape[1] != 2 or not 0 < len(samples) <= 48000 * 31:
                raise ValueError("Expected stereo 48 kHz call audio, at most 31 seconds.")
            # Average the channels without boosting gain, then use libswresample
            # for anti-alias filtering into the usual mono 16 kHz input.
            import av
            mono = samples.mean(axis=1, keepdims=True)
            frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(mono.T), format="fltp", layout="mono")
            frame.sample_rate = rate
            resampler = av.AudioResampler(format="fltp", layout="mono", rate=16000)
            frames = resampler.resample(frame) + resampler.resample(None)
            samples = np.concatenate([f.to_ndarray().T for f in frames], axis=0)
            rate = 16000
        if rate != 16000 or len(samples) > 16000 * 31 or samples.shape[1] != 1:
            raise ValueError("Expected mono 16 kHz audio, at most 31 seconds.")
        models(req, speech=False)
        segments, _ = _stt.transcribe(np.squeeze(samples, axis=1),
                                     language=req.get("language") or None,
                                     initial_prompt=req.get("vocab") or None,
                                     vad_filter=True, beam_size=int(req.get("stt_beam_size") or 1),
                                     condition_on_previous_text=False)
        return {"text": " ".join(s.text.strip() for s in segments).strip()}
    if req["op"] == "synthesize":
        import soundfile as sf
        models(req)
        samples, rate = generate(req, req["text"])
        buf = io.BytesIO()
        subtype=req.get('pcm_format','PCM_16')
        if subtype not in ('PCM_16','PCM_24'):raise ValueError('Unsupported speech PCM format.')
        sf.write(buf, samples, rate, format="WAV", subtype=subtype)
        return {"audio": base64.b64encode(buf.getvalue()).decode("ascii"),
                "seconds": len(samples) / rate}
    if req["op"] == "synthesize_stream":
        if req.get("backend") != "kyutai":
            raise ValueError("Progressive audio currently requires Kyutai.")
        models(req)
        seconds = 0.
        def audio_ready(samples, rate):
            nonlocal seconds
            if cancel is not None and cancel.is_set():
                raise SpeechCancelled()
            seconds += len(samples) / rate
            emit(dict(encode_audio(samples, rate,req.get('pcm_format','PCM_16')), event="audio"))
        extra={'delivery':req['delivery']} if req.get('delivery') is not None else {}
        try:
            _, _, first = _tts.synthesize(req["text"], req.get("style") or "calm", req.get("kyutai_voice"), on_audio=audio_ready,**({'cancel':cancel} if cancel is not None else {}),**extra)
        except InterruptedError:
            if cancel is not None and cancel.is_set():raise SpeechCancelled()
            raise
        return {"seconds": seconds, "first_audio_s": first}
    raise ValueError("Unknown speech operation")


def generate(req, text):
    if req.get("backend") == "kyutai":
        extra={'delivery':req['delivery']} if req.get('delivery') is not None else {}
        samples, rate, _ = _tts.synthesize(text, req.get("style") or "calm", req.get("kyutai_voice"),**extra)
        return samples, rate
    if req.get("backend") == "qwen":
        import re
        text = re.sub(r"\[(?:laugh|chuckle|sigh|gasp|cough|clear throat|sniff)\]", "", text).strip()
        wavs, rate = _tts.generate_voice_clone(text=text, language="English", voice_clone_prompt=_clone_prompt, max_new_tokens=1200)
        return wavs[0], rate
    wav = _tts.generate(text)
    return wav.squeeze().detach().cpu().numpy(), _tts.sr


def main():
    # Windows numeric DLL initialization can query console descriptors. Do
    # this before a background stdin read holds the pipe's runtime lock.
    import importlib
    import importlib.util
    with contextlib.redirect_stdout(sys.stderr):
        for module in ("numpy", "torch", "soundfile", "faster_whisper"):
            if importlib.util.find_spec(module) is not None:
                importlib.import_module(module)
    if os.getenv("ANIMA_VOICE_DEBUG_STACK"):
        import faulthandler
        faulthandler.dump_traceback_later(20, file=sys.stderr)
    protocol = sys.stdout
    work = queue.Queue()
    cancellations = {}
    lock = threading.Lock()
    def reader():
        for line in sys.stdin:
            try:
                req = json.loads(line)
                if req.get("op") == "cancel":
                    with lock:
                        event = cancellations.get(req.get("id"))
                    if event:
                        event.set()
                    continue
                event = threading.Event()
                with lock:
                    cancellations[req.get("id")] = event
                work.put((req, event))
            except (ValueError, AttributeError):
                continue
        work.put(None)
    threading.Thread(target=reader, daemon=True).start()
    def send(result, request_id):
        result["id"] = request_id
        protocol.write(json.dumps(result) + "\n"); protocol.flush()
    while True:
        item = work.get()
        if item is None:
            break
        req, cancel = item
        try:
            # Upstream packages print progress and messages to stdout.
            with contextlib.redirect_stdout(sys.stderr):
                result = handle(req, emit=lambda event: send(event, req.get("id")), cancel=cancel)
            result["id"] = req.get("id")
        except SpeechCancelled:
            result = {"cancelled": True}
        except Exception as e:
            import traceback
            traceback.print_exc(file=sys.stderr)
            result = {"id": req.get("id"), "error": f"{type(e).__name__}: {e}"}
        send(result, req.get("id"))
        with lock:
            cancellations.pop(req.get("id"), None)


if __name__ == "__main__":
    main()
