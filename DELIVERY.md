# Voice delivery

Android Audio settings provide pitch-preserving speed, a same-speaker Kyutai
Desire/Whisper blend, extra section pauses and model guidance. Settings are stored
per paired phone. Preview auditions the chosen voice and position without adding
a conversation or memory turn. Parlor also offers delivery styles and dynamics.

Kyutai uses all 32 audio codebooks. Intermediate Desire/Whisper blends are
experimental. Speed processing uses SoXR VHQ resampling to 48 kHz and Rubber Band
R3 tempo processing. Windows setup is `python engine/setup_delivery.py`; Kyutai
voice setup invokes it automatically. Other platforms need `rubberband-r3` or an
R3-capable `rubberband` on PATH. Speed 100% bypasses tempo processing.

`LIVE_VOICE_SPLIT_REPLIES = False` synthesizes a complete reply in one pass.
At native speed, checked buffers can stream during generation. Tempo-adjusted
phone playback and previews buffer the generation before stretching. Parlor live
playback uses native timing for progressive output. Set the split option to
`True` for independently synthesized bounded sections; extra section pauses apply
in that mode. Silence detection, recognition and the chat turn add latency.

Audio checks cover nonfinite samples, excessive duration and repeated codes.
A generation can retry once before audio is released. After output begins,
a later failure stops rather than replaying the audible prefix. Cancellation
also clears pending preview and playback work. These checks do not evaluate
perceived voice quality.

References, tools, models and auditions remain under `.runtime/`. Restart the
voice server after installing changes. See [VOICE.md](VOICE.md) and
[PHONE.md](PHONE.md) for installation and native playback.
