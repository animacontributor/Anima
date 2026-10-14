# Live calls with your friend on Discord

The first version runs a turn-based conversation inside a continuous **server voice channel**. You finish a phrase, the friend answers and speaks through the same local speech models as Parlor. Audio interrupts playback only after recognition accepts actual words; sustained noise opens a candidate capture but cannot stop speech by itself. Bot calls in a personal DM are not supported; use the DM to control a private server channel.

## Start a call

1. Invite your existing bot to your private server. Give it **View Channel**, **Connect** and **Speak** permissions in the voice channel. Administrator and Message Content Intent are unnecessary for this implementation.
2. Install the transport once: `python engine/setup_discord_voice.py`. On Windows this installs a private Node runtime and pinned packages under `.runtime/`. It does not change global Node or Git settings.
3. Set `DISCORD_VOICE_ENABLED = True` in `engine/config.py` and restart the Discord bridge. The usual Parlor voice setup must also be installed; Kyutai uses `python engine/setup_voice.py --kyutai`.
4. Close Parlor's console and let any active wake or afterglow finish. Join the voice channel yourself.
5. In your **paired DM** with the bot, send `!call CHANNEL_ID`. Enable Discord's Developer Mode to copy a channel ID from its right-click menu. `!call` alone uses `DISCORD_VOICE_CHANNEL_ID`, or the private default saved in `.runtime/discord-voice-settings.json`.
6. Wait for **Ready to listen**, then speak normally. Send `!hangup` in the DM to end the audio connection. `!callstatus` shows its status.

The first version accepts one person: the account paired with the DM. It subscribes only to that account's audio and ends the call if someone else joins, if you leave, or if the bot is moved. It never joins automatically. No call recordings are saved: utterances stay in memory while being transcribed. Recognized words and replies are saved in the ordinary local Discord transcript. Chat copies are off by default; `DISCORD_VOICE_TRANSCRIPTS = True` optionally shows those words in the paired DM.

## Conversation and performance

Calls and DMs share one Discord history, including the letters the friend sent between visits. Hangup keeps that visit available; the bridge's normal afterglow and idle rules still apply. Use `!new` after hangup to close the visit and start a new one.

The existing chat engine supplies identity, memory, tools and reply checks. Thinking uses `LIVE_VOICE_THINK`. Recognition keeps the configured Whisper model, beam search, vocabulary hints and GPU precision. Incoming Discord audio is decoded at 48 kHz, then converted to the recognizer's usual mono 16 kHz with an anti-alias filter. RTP timing preserves pauses and missing-packet durations. Kyutai keeps its existing synthesis settings, with progressive output encoded into Discord's Opus transport.

One live voice front end runs at a time. A process lease prevents duplicate speech model workers. A call holds the brain lease until any in-flight turn finishes; other processes cannot run a brain reply, wake, or unload underneath it. Scheduled heartbeat wakes wait while the call is live, even during a long quiet pause. Manual wakes report that the models are busy. DMs in the same bridge can still use the shared visit. Parlor and Heartbeat must be restarted after installing this change to pick up the coordination code.

Hangup stops Discord playback immediately. An already-running LLM turn can finish in the background; its text is retained and marked as interrupted, and the leases are released after cleanup. A completed playback note says audio was sent to Discord; it does not claim the listener heard it.

## Implementation status

Local tests cover audio timing, format conversion, interruption, paired-account command routing, shared history, model leases and existing voice behavior. The pinned transport and codec load successfully. Reconnects, encryption key changes and latency still need broader live validation.

Incoming timestamps stay attached to individual packets before the readable stream buffers them. A regression test covers delayed audio events followed by unrelated UDP packets; using a shared current timestamp could otherwise discard the buffered speech. The transport log records packet/decode counts, audio levels and completed utterance counts every five seconds, without audio, transcript text, IDs or keys.

The transport uses released `@discordjs/voice` 0.19.2, `discord.js` 14.27.0 and `opusscript` 0.1.1, with a lockfile for their dependencies. DAVE encryption is enabled. [Discord's voice protocol](https://github.com/discord/discord-api-docs/blob/main/developers/topics/voice-connections.mdx) describes the encryption requirements; the [library](https://github.com/discordjs/discord.js/tree/main/packages/voice) implements sending and receiving. Audio reception is not a documented Discord API. This version therefore pins the receiver and its small RTP timing adapter, and fails if its expected timing interface is unavailable.

## If a call fails

- **Permission error:** check the voice channel's permission overrides, not just the bot's server role.
- **Close Parlor / models busy:** close its console after Leave finishes, or wait for the active wake/afterglow. Only opening another browser tab does not release the speech worker.
- **Missing transport:** rerun `python engine/setup_discord_voice.py`.
- **Connection error:** use `!hangup`, then `!call` again. No automatic rejoin occurs after a complete disconnect.
- **Useful local logs:** `.runtime/discord-voice-transport.log` and `.runtime/voice-worker.log`. The bot token travels to the helper over stdin, never in its command line. Do not post raw private logs or transcripts publicly.
