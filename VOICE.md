# Live voice conversations

Parlor, Discord server calls and the paired Android app use the same chat engine,
identity, memory and tools. Speech recognition and synthesis run locally. Replies
wait for the complete model turn; Kyutai then streams one continuous utterance.
This is a turn-based conversation with interruption, rather than native
speech-to-speech.

## Install and start

Install Python 3.12 speech dependencies with `python engine/setup_voice.py --kyutai
--download` for the default Kyutai backend. The isolated environment is
`.runtime/kyutai-env`. The Windows installer also prepares delivery tools.
For Chatterbox, use `python engine/setup_voice.py --download`; for Qwen, add
`--qwen`. Those environments are `.venv-voice` and `.runtime/voice-design-env`.
Set `LIVE_VOICE_BACKEND` to the installed backend in `engine/config.py`, then
restart the voice server. Package and model downloads need network access;
`OFFLINE = True` requires cached weights.

Open Parlor with `bat/parlor.bat` (or its `.command`/`.sh` twin), select a delivery
style or voice sample, and press **start voice**. Pause after a phrase to submit
it. Kyutai uses released voice embeddings; Chatterbox and Qwen can use a local
reference WAV. References and auditions remain under `.runtime/voices/`.
Previewing or choosing a voice does not rewrite identity or create a chat turn.

Kyutai supports Calm, Desire, Happy, Neutral and Whisper conditions, with the
same released speaker. See [DELIVERY.md](DELIVERY.md) for tempo, whisper blend,
pauses and guidance. Audition scripts are `engine/kyutai_auditions.py`,
`engine/voice_auditions.py` and `engine/qwen_voice_auditions.py`.

## Recognition, playback and interruption

The default recognizer is Whisper `large-v3-turbo`, beam size 5, on CPU/int8.
`LIVE_VOICE_STT_DEVICE = "cuda"` uses float16 and needs additional GPU memory.
`LIVE_VOICE_PARK_STT = True` parks CUDA recognition weights in RAM between turns,
then restores the same model and precision. Optional vocabulary hints go in
`LIVE_VOICE_STT_HINT`.

Recognition uses an independent persistent request pipe. Candidate microphone
sound is transcribed while existing speech continues; only accepted words cancel
playback and start a new turn. Rejected noise preserves the current reply and
silence clock. Headphones permit microphone interruption; Android Speaker mode
suppresses capture during playback. Interrupt stops output immediately. Stop
voice, Leave and closing Parlor release the microphone.

`LIVE_VOICE_SPLIT_REPLIES = False` preserves one synthesis pass across the reply.
Set it to `True` for independently synthesized bounded sections. Speech failures
can retry once before audio is released; failures after output begins stop
instead of replaying an audible prefix. Playback receipts describe delivery,
without claiming the listener heard it.

## Context and continuity

The context allocation remains controlled by `NUM_CTX`; this optimization does
not increase it. At 80% occupancy (or conservative estimated usage), the engine
folds older context into the companion's continuity account and keeps the last
12 visible messages, normally six exchanges. Parlor and phone fold before the
next reply or quiet decision, or while idle, so a ready spoken reply does not
wait for summarization. Failed summaries or transcript writes retain the
existing conversation. Playback receipts still reference retained turns.

Voice retrieval adds at most 10 relevant and 2 recent memories within 3,000
characters per message. Identity, keeper pages and journal allowances are
unchanged. Private speak/wait exchanges can leave together at a fold; spoken
continuations remain part of the conversation.

Tool results longer than 12,000 characters are archived under
`memory/tool_results/` and excerpted for voice context. `read_file(path, offset)`
pages through the complete result without repeating an action. If archiving
fails, the full result remains in context. The archives persist locally with
memory; ordinary microphone audio is still not saved.

A context check runs before each main-model chat request, including retries.
It reserves reply space and uses conservative text/image estimates rather than
an exact tokenizer. Actual prompt counts, cached counts when supplied by Ollama,
and estimated category totals remain separate in the numeric diagnostics.
Restart the active voice front end to load these changes.

## Speaking during silence

During an active voice visit, `LIVE_VOICE_INITIATIVE = True` permits a private
model decision after playback completes. The first pause is sampled from a
bounded normal distribution: default mean 25 seconds, standard deviation 6,
bounds 5–90. The model may speak, choose another wait of 3–600 seconds, or remain
quiet until the user speaks. There is no fixed count of successive passages.
Silence does not establish that someone has fallen asleep.

New accepted words, typed input, interruption, Stop or Leave cancel pending
choices. Capture/recognition holds late continuations; accepted words supersede
them. Private choices stay out of visible chat and saved transcripts.
Android uses the PC heartbeat and native foreground service, so scheduling,
capture, delivery and completion work without WebView timers or a lit screen.
Parlor checks through its browser; Discord waits for transport completion.

`LIVE_VOICE_INITIATIVE_MEAN_S`, `LIVE_VOICE_INITIATIVE_STD_S` and
`LIVE_VOICE_INITIATIVE_THINK` control the initial timing and private thinking.
Console diagnostics are controlled by `LIVE_VOICE_INITIATIVE_LOG`,
`LIVE_VOICE_INITIATIVE_LOG_THINKING` and `LIVE_VOICE_INITIATIVE_LOG_INTERVAL_S`.
They can contain proposed speech and private reasons; treat console output as
private. Duration-only performance measurements are in
`.runtime/voice-latency.jsonl`; worker diagnostics are in
`.runtime/voice-worker.log` and `.runtime/voice-recognizer.log`.

One live speech front end runs at a time. Model and brain leases coordinate
Parlor, phone, Discord and Heartbeat. Restart running processes after updating.
See [DISCORD-VOICE.md](DISCORD-VOICE.md), [PHONE.md](PHONE.md) and
[HANDY.md](HANDY.md) for the other integrations.
