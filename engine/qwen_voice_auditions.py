"""Compare native Qwen speech with the mellow voice and a younger adult variant.
Run using .runtime/voice-design-env's Python. Never edits the friend's memory or config.
"""
import base64
import gc
import json
import os
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("HF_HOME", str(ROOT / ".runtime" / "models"))
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def main():
    import config
    if getattr(config, "OFFLINE", False):
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import torch
    import soundfile as sf
    from qwen_tts import Qwen3TTSModel
    import live_voice
    from voice_auditions import REFERENCE_TEXT, PREVIEW_TEXT
    folder = Path(getattr(config, "VOICE_ASSETS_DIR", config.ROOT / ".runtime" / "voices")) / "auditions"
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    mellow = next(v for v in manifest["voices"] if v["label"] == "Warm and mellow")
    free, _ = torch.cuda.mem_get_info()
    if free < 5 * 1024**3:
        raise RuntimeError("Please stop voice playback before making auditions; the active chat model was not unloaded.")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    description = ("A young adult woman, around 20 years old, speaking clear English with a light American accent. "
                   "A warm mellow feminine mid-to-low register, softly rounded resonance and subtle breathiness. "
                   "A youthful, relaxed voice with gentle curiosity, intimate one-to-one delivery, soft natural "
                   "inflections and an unhurried conversational rhythm. Clear, audible speech.")
    model = Qwen3TTSModel.from_pretrained("Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign", device_map="cuda:0",
                                         dtype=torch.bfloat16, attn_implementation="sdpa")
    torch.manual_seed(2450)
    wavs, rate = model.generate_voice_design(text=REFERENCE_TEXT, language="English", instruct=description, max_new_tokens=1200)
    name = stamp + "-young-mellow-reference.wav"
    sf.write(folder / name, wavs[0], rate, subtype="PCM_16")
    seconds = len(wavs[0]) / rate
    if not 5 < seconds < 16:
        raise RuntimeError(f"Reference duration {seconds:.1f}s is outside the planned 5–16s range.")
    print(f"Younger mellow reference ready: {seconds:.1f}s", flush=True)
    del model; gc.collect(); torch.cuda.empty_cache()
    variants = [
        {"id": stamp + "-mellow-qwen", "label": "Warm and mellow · Qwen", "reference": mellow["reference"],
         "description": mellow["description"], "seconds": mellow["seconds"]},
        {"id": stamp + "-young-mellow-qwen", "label": "Younger mellow · Qwen", "reference": name,
         "description": description, "seconds": round(seconds, 2)},
    ]
    try:
        for item in variants:
            started = time.monotonic()
            reference = str(folder / item["reference"])
            live_voice.WORKER.request("warmup", backend="qwen", reference=reference, reference_text=REFERENCE_TEXT)
            started = time.monotonic()
            result = live_voice.WORKER.request("synthesize", backend="qwen", reference=reference,
                                              reference_text=REFERENCE_TEXT, text=PREVIEW_TEXT)
            preview = item["id"] + ".wav"
            (folder / preview).write_bytes(base64.b64decode(result["audio"], validate=True))
            item.update(preview=preview, backend="qwen", reference_text=REFERENCE_TEXT)
            print(f"{item['label']}: {result['seconds']:.1f}s audio, synthesis {time.monotonic() - started:.1f}s", flush=True)
    finally:
        live_voice.WORKER.close()
    manifest["voices"].extend(variants)
    temporary = folder / "manifest.json.part"
    temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    temporary.replace(folder / "manifest.json")
    print("Native Qwen auditions ready. Existing references and selection preserved.", flush=True)


if __name__ == "__main__":
    main()
