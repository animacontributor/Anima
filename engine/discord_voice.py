"""A private, turn-based Discord call using the bridge's history and local voice."""
from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid
import wave

import chat
import config
import doors
import live_voice
import setup_discord_voice
import tools
import voice_lock
import voice_initiative


SETTINGS_FILE = config.ROOT / ".runtime" / "discord-voice-settings.json"


def default_channel() -> str:
    configured = str(getattr(config, "DISCORD_VOICE_CHANNEL_ID", "") or "")
    if configured:
        return configured
    try:
        return str(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")).get("channel_id") or "")
    except (OSError, ValueError):
        return ""


def validate_capture(encoded: str) -> str:
    if len(encoded) > 8_000_000:
        raise ValueError("Call utterance is too large.")
    raw = base64.b64decode(encoded, validate=True)
    with wave.open(io.BytesIO(raw), "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (48000, 2, 2):
            raise ValueError("Expected stereo 48 kHz Discord audio.")
        if not 0 < w.getnframes() <= 31 * 48000 or len(w.readframes(w.getnframes())) != w.getnframes() * 4:
            raise ValueError("Invalid or overlong call audio.")
    return encoded


class Call:
    def __init__(self, bridge, worker=None):
        self.bridge = bridge
        self.worker = worker or live_voice.WORKER
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.ack = threading.Condition()
        self.acked: set[tuple[str, int]] = set()
        self.jobs: dict[str, dict] = {}
        self.active = False
        self.connected = False
        self.epoch = 0
        self.channel = ""
        self.proc = None
        self.log = None
        self.thread = None
        self.pending = queue.Queue(maxsize=2)
        self.ready = threading.Event()
        self.error = ""
        self.brain_lease = None
        self.voice_lease = None
        self.initiative = voice_initiative.Clock()

    def status(self) -> str:
        if self.active:
            return f"Discord call: {'listening' if self.connected else 'connecting'}, channel {self.channel}."
        if self.thread and self.thread.is_alive():
            return "Discord call ended; the current turn is finishing before the models are released."
        return "No Discord call. Join your private voice channel and send !call in this DM."

    def start(self, channel: str, owner: str) -> str:
        if not getattr(config, "DISCORD_VOICE_ENABLED", False):
            raise ValueError("Enable DISCORD_VOICE_ENABLED in engine/config.py and restart the bridge first.")
        if not channel.isdigit() or not 15 <= len(channel) <= 22:
            raise ValueError("Use !call CHANNEL_ID, with the ID of a server voice channel.")
        if not owner.isdigit():
            raise ValueError("Could not identify the paired Discord account.")
        with self.lock:
            if self.active or (self.thread and self.thread.is_alive()):
                return self.status()
            if doors.status("parlor"):
                raise ValueError("Close Parlor's console before starting a live call; this first version uses one voice front end at a time.")
            if getattr(self.bridge, "_glows", 0) or (getattr(self.bridge, "_night", None) and self.bridge._night.is_alive()):
                raise ValueError("Let the bridge finish writing the previous visit down before starting the call.")
            node = setup_discord_voice.node_executable()
            deps = config.ROOT / ".runtime" / "discord-voice"
            if not node or not (deps / "node_modules" / "@discordjs" / "voice").is_dir():
                raise ValueError("Run python engine/setup_discord_voice.py to install the Discord call transport.")
            self.brain_lease = voice_lock.Lease("brain").acquire()
            try:
                self.voice_lease = voice_lock.Lease("live-voice-models").acquire()
                self.pending = queue.Queue(maxsize=2)
                self.ready.clear(); self.error = ""; self.epoch += 1
                self.active = True; self.connected = False; self.channel = channel
                self.acked.clear(); self.jobs.clear()
                self.log = (config.ROOT / ".runtime" / "discord-voice-transport.log").open("a", encoding="utf-8")
                env = dict(os.environ, ANIMA_DISCORD_VOICE_DEPS=str(deps))
                self.proc = subprocess.Popen([str(node), str(Path(__file__).with_name("discord_voice_transport.cjs"))],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                    text=True, encoding="utf-8", cwd=config.ROOT, env=env,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                doors.mark("discord-call", "connecting", channel=channel)
                threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
                self._send({"op": "init", "token": self.bridge.token, "owner": owner, "channel": channel,
                    "speech": {"silenceMs": getattr(config, "LIVE_VOICE_SILENCE_MS", 1000),
                        "minSpeechMs": getattr(config, "LIVE_VOICE_MIN_SPEECH_MS", 250),
                        "interruptMs": getattr(config, "LIVE_VOICE_INTERRUPT_MS", 350),
                        "threshold": getattr(config, "LIVE_VOICE_THRESHOLD", .018),
                        "maxSeconds": min(30, getattr(config, "LIVE_VOICE_MAX_SECONDS", 30))}})
                self.thread = threading.Thread(target=self._loop, daemon=True)
                self.thread.start()
            except Exception:
                self.active = False
                self._cleanup()
                raise
        return "Joining your voice channel. I'll send a ready notice after the speech models warm up."

    def _send(self, message):
        with self.write_lock:
            if not self.proc or self.proc.poll() is not None:
                raise live_voice.VoiceError("Discord voice transport closed.")
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()

    def _notice(self, text):
        # Network sends never block the transport reader or its audio acknowledgments.
        def send():
            try:
                self.bridge._try_send(text, markdown=False)
            except Exception:
                pass
        threading.Thread(target=send, daemon=True).start()

    def _read(self, proc):
        try:
            for line in proc.stdout:
                if proc is not self.proc:
                    return  # A previous helper must never close a restarted call.
                if len(line) > 12_000_000:
                    raise ValueError("Oversized Discord audio event.")
                event = json.loads(line)
                kind = event.get("event")
                if kind == "ready":
                    self.connected = True; self.ready.set()
                    doors.mark("discord-call", "live", channel=self.channel)
                elif kind == "audio_ack":
                    with self.ack:
                        if len(self.acked) < 256:
                            self.acked.add((event.get("job"), event.get("index")))
                        self.ack.notify_all()
                elif kind == "interrupt":
                    voice_initiative.console_log("discord", "Microphone activity detected; checking for accepted words before interruption.")
                elif kind == "utterance" and self.active:
                    audio = validate_capture(event["audio"])
                    with self.lock:
                        item = (self.epoch, audio)
                    try:
                        self.pending.put_nowait(item)
                    except queue.Full:
                        self._notice("The call is still catching up. Please wait for the current reply before another turn.")
                elif kind == "playback":
                    threading.Thread(target=self._receipt, args=(event,), daemon=True).start()
                elif kind == "error":
                    self.error = str(event.get("error") or "Discord voice error")
                    self.interrupt()  # Wake a blocked audio producer and cancel this speech roll.
                    self._notice("Call: " + self.error)
                elif kind == "closed":
                    if self.active:
                        self._notice("Call ended: " + str(event.get("reason") or "disconnected"))
                    self.close(send=False)
                    break
        except (OSError, ValueError, KeyError) as e:
            if proc is self.proc:
                self.error = f"Voice transport error: {type(e).__name__}"
                if self.active:
                    self._notice(self.error)
        finally:
            if proc is self.proc:
                self.ready.set()
                self.close(send=False)

    def interrupt(self):
        self.initiative.interrupt()
        with self.lock:
            self.epoch += 1
        with self.ack:
            self.ack.notify_all()

    def close(self, send=True):
        self.initiative.stop()
        with self.lock:
            was_active = self.active
            self.active = False; self.connected = False; self.epoch += 1
        if send and was_active:
            try:
                self._send({"op": "hangup"})
            except (OSError, live_voice.VoiceError):
                pass
        with self.ack:
            self.ack.notify_all()
        try:
            self.pending.put_nowait(None)
        except queue.Full:
            pass

    def _loop(self):
        try:
            if not self.ready.wait(60) or not self.connected or not self.active:
                raise live_voice.VoiceError(self.error or "Discord connection timed out.")
            self.worker.request("warmup")
            if not self.active:
                return
            self.initiative.start(source="discord")
            monitor_stopped = threading.Event()
            threading.Thread(target=self.initiative.watch, args=(monitor_stopped,), daemon=True).start()
            self._notice("Ready to listen. Only your audio is used; send !hangup here to end the call.")
            while self.active:
                try:
                    item = self.pending.get(timeout=.5)
                except queue.Empty:
                    self._initiative()
                    continue
                if item is None:
                    break
                epoch, audio = item
                if epoch != self.epoch:
                    continue
                try:
                    self._turn(epoch, audio)
                except Exception as e:
                    try:
                        self._send({"op": "stop"})
                    except (OSError, live_voice.VoiceError):
                        pass
                    for job in list(self.jobs):
                        self._receipt({"job": job, "partial": True, "reason": "voice generation failed"})
                    if self.active:
                        self._notice(f"Call turn failed: {type(e).__name__}: {e}")
        except Exception as e:
            if self.active:
                self._notice(f"Call could not start: {e}")
        finally:
            self.close()
            if 'monitor_stopped' in locals():
                monitor_stopped.set()
            self._cleanup()

    def _turn(self, epoch, audio):
        transcript = self.worker.request("transcribe", audio=audio, discord_input=True,
            vocab=f"Names: {config.USER_NAME}, {chat.friend_name()}.").get("text", "").strip()
        if not transcript or not self.active or epoch != self.epoch:
            if not transcript:
                self.initiative.empty_capture()
            return
        self.interrupt()
        epoch = self.epoch
        self.initiative.heard()
        if self.jobs:
            self._send({"op": "stop"})
        events = []
        with self.bridge.lock:
            if not self.active or epoch != self.epoch:
                return
            beginning = len(self.bridge.history)
            reply = chat.one_turn(self.bridge.history, transcript, images=self.bridge.attached or None,
                on_event=lambda k, p: events.append((k, p)), mode="discord_voice")
            reply = chat.strip_voice_delivery(reply)
            self.bridge.attached = []
            for turn in self.bridge.history[beginning:]:
                if turn.get("role") == "assistant" and turn.get("content"):
                    turn["content"] = chat.strip_voice_delivery(turn["content"])
            assistant = next((t for t in reversed(self.bridge.history) if t.get("role") == "assistant"), None)
            if assistant:
                assistant["_voice_delivery"] = "Reply generated; Discord speech has not yet been sent."
            tokens = next((p for k, p in events if k == "tokens"), None)
            if tokens:
                self.bridge.last_tokens = tokens
            self.bridge.last_activity = time.time()
            self.bridge._checkpoint()
        if not self.active or epoch != self.epoch:
            if assistant:
                self._receipt({"turn": assistant, "partial": True, "reason": "interrupted before speech"})
            return
        if getattr(self.bridge, "show_thinking", False):
            thinking = "\n\n".join(str(p) for k, p in events if k == "thinking")
            if thinking:
                self.bridge._try_send("💭 " + thinking[:4000], markdown=False)
        if getattr(config, "DISCORD_VOICE_TRANSCRIPTS", False):
            self.bridge._try_send("🎙 " + transcript, markdown=False)
            self.bridge._send_reply(reply)
        for kind, payload in events:
            if kind == "note":
                self.bridge._try_send("⚠ " + str(payload), markdown=False)
            elif kind == "tool" and self.bridge.show_tools:
                self.bridge._try_send(f"· {payload['name']} → {payload['result']}", markdown=False)
        if getattr(self.bridge, "show_tokens", False) and tokens:
            self.bridge._try_send(tokens["line"], markdown=False)
        self.bridge._carry_voice("")  # Explicit speak calls still travel as separate voice notes.
        if not self.active or epoch != self.epoch:
            if assistant:
                self._receipt({"turn": assistant, "partial": True, "reason": "interrupted before speech"})
            return
        self._speak(reply, assistant, epoch)
        self.bridge.fold_if_due()

    def _initiative(self):
        self.initiative.report_client({"blocked": [code for code, value in
            (("playback", self.jobs), ("queued", not self.pending.empty())) if value],
            "phase": "speaking" if self.jobs else "listening", "queued_turns": self.pending.qsize()})
        self.initiative.request_received()
        if not self.active or not self.connected or self.jobs or not self.pending.empty():
            return
        if not self.bridge.lock.acquire(blocking=False):
            return
        try:
            claim = self.initiative.claim()
            if claim is None:
                return
            epoch = self.epoch
            try:
                decision, additions = voice_initiative.decide(self.bridge.history, claim[2], mode="discord_voice")
            except Exception as error:
                voice_initiative.console_log("discord", f"Decision failed: {type(error).__name__}: {error}; retry in 60s.")
                self.initiative.resolve(claim, {"next_check_seconds": 60}, lambda: None)
                return
            assistant = None

            def commit():
                nonlocal assistant
                self.bridge.history.extend(additions)
                if decision["text"]:
                    assistant = self.bridge.history[-1]
                    assistant["_voice_delivery"] = "Continuation generated; Discord speech has not yet been sent."
                    self.bridge.last_activity = time.time()
                if additions:
                    self.bridge._checkpoint()

            accepted = self.initiative.resolve(claim, decision, commit)
        finally:
            self.bridge.lock.release()
        if accepted and assistant and self.active and epoch == self.epoch:
            try:
                if getattr(config, "DISCORD_VOICE_TRANSCRIPTS", False):
                    self.bridge._send_reply(decision["text"])
                self._speak(decision["text"], assistant, epoch)
                self.bridge.fold_if_due()
            except Exception:
                self.interrupt()
                try:
                    self._send({"op": "stop"})
                except (OSError, live_voice.VoiceError):
                    pass

    def _speak(self, reply, assistant, epoch):
        if reply and self.active and epoch == self.epoch:
            job = uuid.uuid4().hex
            self.jobs[job] = assistant
            self.initiative.speaking(job)
            self._send({"op": "begin", "job": job})
            index = 0

            def receive(chunk):
                nonlocal index
                if not self.active or epoch != self.epoch:
                    return False
                key = (job, index)
                self._send({"op": "audio", "job": job, "index": index, "audio": chunk["audio"]})
                index += 1
                deadline = time.monotonic() + 30
                with self.ack:
                    while key not in self.acked and self.active and epoch == self.epoch:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise live_voice.VoiceError("Discord playback stopped accepting audio.")
                        self.ack.wait(min(remaining, .5))
                    self.acked.discard(key)
                return self.active and epoch == self.epoch

            if getattr(config, "LIVE_VOICE_BACKEND", "kyutai") == "kyutai":
                result = self.worker.request("synthesize_stream", text=reply, on_audio=receive)
                if result.get("audio") and not index:
                    receive(result)
            else:
                for part in live_voice.speech_chunks(reply):
                    if not receive(self.worker.request("synthesize", text=part)):
                        break
            if self.active and epoch == self.epoch:
                self._send({"op": "end", "job": job})
            else:
                self._send({"op": "stop"})

    def _receipt(self, event):
        turn = event.get("turn") or self.jobs.pop(event.get("job"), None)
        if not turn:
            return
        with self.bridge.lock:
            if any(t is turn for t in self.bridge.history):
                turn["_voice_delivery"] = (
                    "Discord playback interrupted; the reply may not have been heard in full. "
                    if event.get("partial") else
                    "Speech sent through the Discord voice connection; hearing it on the client is not confirmed. "
                ) + str(event.get("reason") or "")
                self.bridge._checkpoint()
                self.initiative.finished(event.get("job"), interrupted=bool(event.get("partial")))

    def _cleanup(self):
        # Keep the model leases until an in-flight model turn has finished.
        try:
            self.worker.close()
            if self.proc:
                if self.proc.poll() is None:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.proc.kill(); self.proc.wait(timeout=3)
                for stream in (self.proc.stdin, self.proc.stdout):
                    if stream:
                        stream.close()
                self.proc = None
            if self.log:
                self.log.close(); self.log = None
        finally:
            doors.unmark("discord-call")
            for lease in (self.voice_lease, self.brain_lease):
                if lease:
                    lease.release()
            self.voice_lease = self.brain_lease = None
