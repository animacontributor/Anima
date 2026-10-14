"""Render official Kyutai US female expressive voices as local previews."""
import base64
import json
import time
from datetime import datetime
from pathlib import Path
import config
import live_voice


def main():
    folder = Path(getattr(config, "VOICE_ASSETS_DIR", config.ROOT / ".runtime" / "voices")) / "auditions"
    folder.mkdir(parents=True, exist_ok=True)
    listing = folder / "manifest.json"
    manifest = json.loads(listing.read_text(encoding="utf-8")) if listing.exists() else {"voices": []}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    text = "Hello. Take your time. I'm listening. We can just stay with this for a moment, if you'd like."
    additions = []
    try:
        live_voice.WORKER.request("warmup", backend="kyutai", style="desire")
        for style, label in (("desire", "Desire"), ("calm", "Calming"), ("happy", "Happy"), ("neutral", "Default"), ("whisper", "Whisper")):
            start = time.monotonic()
            result = live_voice.WORKER.request("synthesize", backend="kyutai", style=style, text=text)
            name = f"{stamp}-kyutai-{style}-us-f.wav"
            (folder / name).write_bytes(base64.b64decode(result["audio"], validate=True))
            additions.append({"id": f"{stamp}-kyutai-{style}", "label": label + " (US, f) · Kyutai",
                              "reference": name, "preview": name, "backend": "kyutai", "style": style})
            print(f"{label}: audio {result['seconds']:.2f}s, synthesis {time.monotonic()-start:.2f}s", flush=True)
    finally:
        live_voice.WORKER.close()
    manifest["voices"].extend(additions)
    pending = folder / "manifest.json.part"
    pending.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    pending.replace(listing)


if __name__ == "__main__":
    main()
