"""Generate original, local reference voices and Chatterbox listening previews.

Run with .runtime/voice-design-env's Python (requirements-voice-design.txt).
This script never calls the friend's chat model or edits identity/memory/config.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("HF_HOME", str(ROOT / ".runtime" / "models"))
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

REFERENCE_TEXT = ("Hey, Alex. It's good to hear your voice. Tell me how your evening's been. "
                  "We don't have to make a plan. We can just talk for a while.")
PREVIEW_TEXT = ("Hey, Alex. Take your time. I'm listening. "
                "We can just stay with this for a moment, if you'd like.")
PROFILES = [
    ("velvet", "Soft and close", "An adult woman's voice speaking clear conversational English with a light American accent. "
     "A warm low-to-mid pitch, soft rounded resonance, and gentle breathiness. Relaxed, intimate one-to-one delivery, "
     "a small smile in the voice, natural pauses, subtle melodic inflection. Clear audible speech at an unhurried pace."),
    ("airy", "Light and airy", "An adult woman's voice speaking clear conversational English with a light American accent. "
     "A light feminine mid-register, airy and slightly breathy, warm and tender. Calm intimate conversational delivery, "
     "quiet curiosity, gentle rises and falls in pitch, relaxed pacing, clear words and natural pauses."),
    ("mellow", "Warm and mellow", "An adult woman's voice speaking clear conversational English with a light American accent. "
     "A soft lower feminine register, warm and smooth with a hint of huskiness and breath. Close, intimate and thoughtful "
     "delivery, gentle warmth, natural conversational rhythm, expressive but subtle inflection and a relaxed pace."),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-chatterbox", action="store_true", help="also audition these references through the working voice model")
    args = parser.parse_args()
    import config
    if getattr(config, "OFFLINE", False):
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import torch
    import soundfile as sf
    from qwen_tts import Qwen3TTSModel
    if not torch.cuda.is_available():
        raise RuntimeError("A CUDA voice-design environment is required for these auditions.")
    free, _ = torch.cuda.mem_get_info()
    print(f"Available VRAM: {free / 1024**3:.1f} GiB", flush=True)
    if free < 5 * 1024**3:
        raise RuntimeError("Not enough spare VRAM to design voices alongside the friend; stop voice playback and try again. No models were unloaded.")
    model = Qwen3TTSModel.from_pretrained("Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign", device_map="cuda:0",
                                        dtype=torch.bfloat16, attn_implementation="sdpa")
    folder = Path(getattr(config, "VOICE_ASSETS_DIR", ROOT / ".runtime" / "voices")) / "auditions"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    manifest = []
    for index, (key, label, description) in enumerate(PROFILES):
        torch.manual_seed(2400 + index)
        started = time.monotonic()
        wavs, rate = model.generate_voice_design(text=REFERENCE_TEXT, language="English", instruct=description,
                                                max_new_tokens=1200)
        name = f"{stamp}-{key}-reference.wav"
        sf.write(folder / name, wavs[0], rate, subtype="PCM_16")
        seconds = len(wavs[0]) / rate
        if seconds <= 5:
            raise RuntimeError(f"{label} produced too little audio to use as a reference.")
        manifest.append({"id": f"{stamp}-{key}", "label": label, "description": description,
                         "reference": name, "preview": None, "seconds": round(seconds, 2)})
        print(f"{label}: {seconds:.1f}s of reference audio, generated in {time.monotonic() - started:.1f}s", flush=True)
    del model
    import gc
    gc.collect(); torch.cuda.empty_cache()
    if args.render_chatterbox:
        import base64
        import live_voice
        try:
            for item in manifest:
                reference = str(folder / item["reference"])
                live_voice.WORKER.request("reference_info", path=reference)
                live_voice.WORKER.request("warmup", reference=reference)
                result = live_voice.WORKER.request("synthesize", reference=reference, text=PREVIEW_TEXT)
                preview = item["id"] + "-chatterbox.wav"
                (folder / preview).write_bytes(base64.b64decode(result["audio"], validate=True))
                item["preview"] = preview
                print(f"{item['label']}: Chatterbox preview ready", flush=True)
        finally:
            live_voice.WORKER.close()
    # Publish the listing atomically after all requested previews succeed.
    temporary = folder / "manifest.json.part"
    temporary.write_text(json.dumps({"reference_text": REFERENCE_TEXT, "preview_text": PREVIEW_TEXT, "voices": manifest}, indent=2), encoding="utf-8")
    temporary.replace(folder / "manifest.json")
    print(f"Auditions ready: {folder}", flush=True)


if __name__ == "__main__":
    main()
