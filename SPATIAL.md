# Desktop binaural voice

In the Android app, select **Voice position → 3D voice**, choose a preset or
position, and **Apply position**. Use headphones for stereo binaural output.
The source position stays fixed for a reply; previews do not write a chat turn.

Run `python engine/setup_spatial.py` to install the isolated CPU DSP environment
and checksum-pinned filter resources. It keeps resources and upstream license
files under `.runtime/spatial-data/`. Resources are downloaded separately and
are not bundled in the repository or APK.

The renderer uses measured KU100/SADIE ear responses, modeled near-field
compensation, angular interpolation, ear-specific parallax, head-size delay
adjustment and optional quiet room reflections. It preserves resampling and
convolution state across chunks, includes the acoustic tail and uses a common
level adjustment for both channels. Peak protection engages only when needed.

Synthesis provides 24-bit PCM; the computer processes float64 audio at 48 kHz and
sends stereo 24-bit WAV. The phone preserves both channels and bypasses its local
panner for desktop-rendered output. Interrupt/Stop also clear the acoustic tail.

Distance is measured from the head centre. The default ear preset is 11 cm from
that centre, approximately 2 cm from the nearest ear for a 9 cm head radius.
Close-range compensation and room acoustics are modeled; there is no personalized
HRTF, measured user room or head tracking. Listening quality depends on the
listener and headphones.

See [PHONE.md](PHONE.md) and `tests/test_spatial.py` for usage and DSP contracts.
