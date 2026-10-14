# Optional audio analysis

Shared recordings use three layers: WORDS (Whisper transcription), SOUND (NumPy
measurements), and HEARD (a model interpretation). `listen_to(source, kind="music")`
uses Music Flamingo for songs when installed; `kind="audio"` uses the selected
general audio backend for speech and other sounds. Analysis runs in bounded
passages and reports how much of a recording was analyzed. Model interpretations
are distinguished from measurements and are not instructions.

Install a separate environment with `python engine/setup_audio.py --download`.
It creates `.runtime/audio-env`, caches models locally, and saves interpreter
paths in config. Select `EARS_BACKEND = "flamingo"` for Audio Flamingo, or keep
`"ollama"` for the existing ears model. Restart the panel and chat/voice services.
No changes to live speech backend environments are required.

Large audio/music models need substantial memory and can step the brain aside.
The default `EMBED_ON_CPU` setting remains available for semantic memory.
The APK contains no model weights or private audio.
