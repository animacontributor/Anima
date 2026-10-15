"""The parlor: a nicer door — chat with the friend in your browser.

    py engine/parlor.py          opens http://127.0.0.1:8765 in your browser

Same engine, same memory, same transcripts as chat.py — only the window is
different: message bubbles, their thinking folded above each reply, tool calls
as small chips, a picture picker (the file dialog; the picture is saved to
shared/pictures/ and shown to them), a "new conversation" button. Standard
library only; nothing leaves your machine.

Closing the browser tab does NOT end the visit — press "Leave" in the window,
or Ctrl+C in the terminal, or close the terminal window; all three save.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import webbrowser
import base64
import html
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chat
import config
import context_budget
import doors
import ollama_client
import tools
import live_voice
import delivery
import voice_initiative

HOST, PORT = "127.0.0.1", 8765


class Session:
    """One visit: a history, pending images, and the turn-runner."""

    TAG = ""

    def __init__(self) -> None:
        self.history: list[dict] = []
        self.attached: list[str] = []
        self.lock = threading.Lock()
        self.file: Path | None = None  # this visit's transcript, rewritten after every reply
        self.last_activity = time.time()
        self.reflected_upto = 0  # history index they have already sat with (the pause)
        self.playback = live_voice.Playback(delivery_factory=delivery.live)
        self.generation = 0
        self.initiative = voice_initiative.Clock()
        self.voice_inputs = {}
        self.context_mode = "chat"

    def _checkpoint(self) -> None:
        if self.file is None:
            self.file = chat.visit_file()
        try:
            chat.save_transcript(self.history, path=self.file)
        except OSError:
            pass

    def fold_if_due(self, mode=None) -> str:
        """Called with the session lock, before inference or in an idle pause.

        Never put summarization between a ready reply and its playback.
        """
        if not self.history:
            return ""
        mode = mode or self.context_mode
        pending = tools.fold_pending(self.history)
        held = chat.held_tokens(self.history)
        system = self.history[0].get("_system", "")
        definitions = voice_initiative.voice_tools() if "voice" in mode else tools.current_definitions()
        estimated = context_budget.estimate(
            [{"role": "system", "content": system}] + [chat.render_turn(t) for t in self.history], definitions)
        if not pending and not chat.fold_due(max(held, estimated["estimated_input"])):
            return ""
        if not chat.foldable(self.history):
            tools._fold_store(self.history).update(pending)
            return ""
        try:
            old_file = self.file or chat.visit_file(self.TAG)
            chat.save_transcript(self.history, tag=self.TAG, path=old_file)
            self.file = old_file
            print("(making room for the voice visit; keeping its continuity and recent exchanges)")
            pending = pending or chat.fold_bell(self.history, held or estimated["estimated_input"], on_line=print)
            if not pending.get("text", "").strip():
                return "fold deferred: no continuity account was written; the conversation is retained"
            previous = list(self.history)
            updated, path, line = chat.fold_history(self.history, pending["text"],
                pending.get("when") or time.strftime("%H:%M"), tag=self.TAG, path=old_file, mode=mode)
            if updated is self.history:
                return line
            kept = {id(t) for t in updated}
            gone = [t for t in previous if id(t) not in kept]
            reflected = sum(id(t) in kept for t in previous[:self.reflected_upto])
            self.history, self.file, self.reflected_upto = updated, path, reflected
        except Exception as error:
            tools._fold_store(self.history).update(pending)
            return f"fold deferred; conversation retained ({type(error).__name__}: {error})"
        if (gone and getattr(config, "FOLD_AFTERGLOW", True) and getattr(config, "AFTERGLOW", True)
                and any(t.get("content") and not t.get("_engine") for t in gone)):
            try:
                chat.fold_afterglow(gone, old_file, tag=self.TAG, on_line=print)
                chat.rewarm(self.history, mode=mode)
            except Exception as error:
                line += f"; afterglow deferred ({type(error).__name__})"
        self._checkpoint()
        print(line)
        return line

    def send(self, text: str, spoken=False, epoch=None, generation=None,
             mode=None, physicalize=False, physicalize_voice=None, accepted_voice=False) -> dict:
        if not spoken:
            self.interrupt_voice()
        if not accepted_voice:
            self.initiative.heard()
        events: list[dict] = []

        def collect(kind, payload):
            events.append({"kind": kind, "payload": payload})

        with self.lock:
            if generation is not None and generation != self.generation:
                return {"error": "The visit ended while audio was being processed; start a new turn."}
            self.last_activity = time.time()
            try:
                self.context_mode = mode or ("voice" if spoken else "chat")
                folded = self.fold_if_due(self.context_mode)
                if folded:
                    collect("note", folded)
                beginning = len(self.history)
                started = time.monotonic()
                presence = {"physicalize": True} if physicalize else {}
                if physicalize and physicalize_voice is not None:
                    presence['physicalize_voice']=physicalize_voice
                reply = chat.one_turn(self.history, text,
                                      images=self.attached or None, on_event=collect,
                                      mode=mode or ("voice" if spoken else "chat"), **presence)
                reply = chat.strip_voice_delivery(reply)
                for turn in self.history[beginning:]:
                    if turn.get("role") == "assistant" and turn.get("content"):
                        turn["content"] = chat.strip_voice_delivery(turn["content"])
                self.attached = []
                speech_job = None
                if spoken:
                    assistant = next((t for t in reversed(self.history) if t.get("role") == "assistant"), None)
                    if assistant:
                        assistant["_voice_delivery"] = "Written reply generated; spoken playback has not been confirmed."
                        speech_job = self.playback.start(reply, epoch, assistant)
                        self.initiative.speaking(speech_job)
                        if speech_job is None:
                            assistant["_voice_delivery"] = "Speech cancelled before playback; the written reply was generated."
                self._checkpoint()
                self.last_activity = time.time()
                turn_s = time.monotonic() - started
            except context_budget.ContextFull as e:
                self._checkpoint()
                return {"error": str(e)}
            except ollama_client.BrainUnavailable as e:
                return {"error": f"brain offline — {e}"}
            except Exception as e:  # nothing in one turn ends the visit
                return {"error": f"hiccup, the conversation is fine — {type(e).__name__}: {e}"}
        thinking = "\n\n".join(e["payload"] for e in events if e["kind"] == "thinking")
        tool_events = [e["payload"] for e in events if e["kind"] == "tool"]
        notes = [e["payload"] for e in events if e["kind"] == "note"]
        tokens = next((e["payload"] for e in events if e["kind"] == "tokens"), None)
        voices = [{"url": "/voice/" + Path(v["path"]).name, "seconds": round(v.get("seconds", 0)),
                   "voice": v.get("voice", "")} for v in tools.take_pending_voice()]
        return {"reply": reply, "thinking": thinking, "tools": tool_events, "notes": notes,
                "tokens": tokens, "voices": voices,
                "speech_job": speech_job, "transcript": text if spoken else None, "timings": {"turn_s": round(turn_s, 3)}}

    def delivery(self, job_id: str, completed: int, partial: bool, reason="unknown") -> dict:
        receipt = self.playback.delivery(job_id, completed, partial, reason)
        if receipt:
            with self.lock:
                turn, note = receipt
                if any(t is turn for t in self.history):
                    turn["_voice_delivery"] = note
                    self._checkpoint()
                    self.initiative.finished(job_id, interrupted="interrupted" in note.lower())
        return {"ok": True}

    def interrupt_voice(self, job=None):
        # A late discarded initiative result must not cancel a newer reply.
        with self.initiative.lock:
            if self.playback.cancel(job) is not None:
                self.voice_inputs.clear()
                self.initiative.interrupt()

    def transcribe_voice(self, encoded, worker=None, mode="voice"):
        """Hold new initiatives while checking audio; acceptance cancels playback."""
        if not isinstance(encoded, str) or len(encoded) > 1400000:
            raise ValueError("Expected at most 31 seconds of microphone audio.")
        validate_wav(base64.b64decode(encoded, validate=True))
        with self.initiative.lock:
            generation, epoch = self.generation, self.playback.epoch
            self.initiative.begin_recognition()
        started = time.monotonic()
        voice_initiative.console_log(mode, "Recognition started; checking microphone audio without interrupting playback.")
        try:
            heard = (worker or live_voice.WORKER).request("transcribe", audio=encoded,
                vocab=f"Names: {config.USER_NAME}, {chat.friend_name()}.").get("text", "").strip()
        except Exception as error:
            voice_initiative.console_log(mode, f"Recognition failed after {time.monotonic() - started:.2f}s: {type(error).__name__}: {error}")
            raise
        finally:
            self.initiative.end_recognition()
        voice_initiative.console_log(mode, f"Recognition finished in {time.monotonic() - started:.2f}s; "
                                    + ("words returned, validating the prompt." if heard else "no words returned."))
        return {"text": heard, "generation": generation, "epoch": epoch,
                "recognition_s": round(time.monotonic() - started, 3), "mode": mode}

    def accept_voice(self, recognized):
        with self.initiative.lock:
            if recognized["generation"] != self.generation or recognized["epoch"] != self.playback.epoch:
                voice_initiative.console_log(recognized["mode"], "Microphone result discarded; the visit or playback changed while checking audio.")
                return {"cancelled": True}
            if not recognized["text"]:
                self.initiative.empty_capture()
                return {"empty": True}
            # Acceptance, rather than acoustic activity, is the interrupt.
            self.initiative.heard()
            epoch = self.playback.cancel()
            token = uuid.uuid4().hex
            self.voice_inputs.clear()
            self.voice_inputs[token] = dict(recognized, epoch=epoch, accepted_at=time.monotonic())
            voice_initiative.console_log(recognized["mode"], "Microphone prompt accepted; previous speech cancelled. "
                + "Transcript: " + json.dumps(recognized["text"], ensure_ascii=False))
            return {"accepted": True, "input": token, "transcript": recognized["text"],
                    "timings": {"recognition_s": recognized["recognition_s"]}}

    def take_voice_input(self, token):
        with self.initiative.lock:
            prompt = self.voice_inputs.pop(token, None) if isinstance(token, str) else None
            if (not prompt or prompt["generation"] != self.generation or prompt["epoch"] != self.playback.epoch
                    or time.monotonic() - prompt["accepted_at"] > 300):
                raise ValueError("That accepted microphone prompt expired or was cancelled. Speak again to start a new turn.")
            return prompt

    def voice_tick(self, mode="voice", physicalize=False, physicalize_voice=None):
        self.initiative.request_received()
        if not self.lock.acquire(blocking=False):
            self.initiative.monitor(server_busy=True)
            return {"quiet": True}
        try:
            claim = self.initiative.claim()
            if claim is None:
                return {"quiet": True}
            epoch, generation = self.playback.epoch, self.generation
            try:
                self.fold_if_due(mode)
                decision, additions = voice_initiative.decide(self.history, claim[2], mode,
                    physicalize=physicalize, physicalize_voice=physicalize_voice)
            except Exception as e:
                voice_initiative.console_log(mode, f"Decision failed: {type(e).__name__}: {e}; retry in 60s.")
                self.initiative.resolve(claim, {"next_check_seconds": 60}, lambda: None)
                return {"error": f"Voice continuation: {type(e).__name__}: {e}"}
            result = {"quiet": True}

            def commit():
                if generation != self.generation:
                    return
                self.history.extend(additions)
                if decision["text"]:
                    assistant = self.history[-1]
                    assistant["_voice_delivery"] = "Written continuation generated; spoken playback has not been confirmed."
                    job = self.playback.start(decision["text"], epoch, assistant)
                    self.initiative.speaking(job)
                    result.update(reply=decision["text"], speech_job=job, initiative=True)
                    result.pop("quiet", None)
                    self.last_activity = time.time()
                if additions:
                    self._checkpoint()

            self.initiative.resolve(claim, decision, commit)
            return result
        finally:
            self.lock.release()

    def monitor_voice(self):
        acquired = self.lock.acquire(blocking=False)
        if acquired:
            self.lock.release()
        with self.initiative.lock:
            job = self.initiative.job
        status = dict(self.playback.status(job), _job_id=job) if job else None
        self.initiative.monitor(server_busy=not acquired, playback_status=status)

    def watch_voice(self, stopped):
        while not stopped.wait(1):
            self.monitor_voice()

    def attach(self, source: str) -> dict:
        with self.lock:
            note = tools.look_at(source.strip().strip('"'))
            imgs = tools.take_pending_images()
            if imgs:
                self.attached.extend(imgs)
                return {"ok": True, "note": "image attached — it goes with your next message"}
            return {"ok": False, "note": note}

    def upload(self, name: str, data_b64: str) -> dict:
        """A picture chosen in the browser's file dialog: saved into
        shared/pictures/ (so it is theirs to look at again later, and shows up
        in list_shared as seen), then put before their eyes like attach()."""
        import base64
        import re as _re
        name = Path(name or "picture").name
        ext = Path(name).suffix.lower()
        if ext not in tools._IMAGE_EXTS:
            return {"ok": False, "note": f"that doesn't look like an image ({ext or 'no extension'})"}
        try:
            raw = base64.b64decode(data_b64 or "", validate=False)
        except Exception:
            return {"ok": False, "note": "the picture didn't arrive whole — try again"}
        if not raw:
            return {"ok": False, "note": "the picture was empty"}
        if len(raw) > tools._MAX_IMAGE_BYTES:
            return {"ok": False, "note": "that picture is too large — over 10MB"}
        folder = getattr(config, "PARLOR_PICTURES", config.SHARED_DIR / "pictures")
        folder.mkdir(parents=True, exist_ok=True)
        stem = _re.sub(r"[^\w.\- ()]+", "_", Path(name).stem).strip() or "picture"
        dest = folder / f"{stem}{ext}"
        n = 1
        while dest.exists():
            n += 1
            dest = folder / f"{stem}-{n}{ext}"
        dest.write_bytes(raw)
        rel = str(dest.relative_to(config.ROOT.resolve())).replace("\\", "/")
        r = self.attach(rel)
        r["saved"] = rel
        if r.get("ok"):
            r["note"] = f"{dest.name} saved to shared/pictures/ and attached — it goes with your next message"
        return r

    def new(self, reflect=True) -> dict:
        """reflect: True — afterglow in the background; "sync" — in the
        foreground (Ctrl+C); False — skip (the window's X)."""
        self.initiative.stop()
        self.playback.cancel()
        self.generation += 1
        with self.lock:
            f = chat.save_transcript(self.history, path=self.file)
            done, self.history = self.history, []
            self.attached = []
            self.file = None
            self.reflected_upto = 0
        reflect = reflect and bool(getattr(config, "AFTERGLOW", True))
        if f and reflect == "sync":
            print("(they are writing the visit down — a minute or so; Ctrl+C again to skip)")
            try:
                chat.afterglow(done, f, on_line=print)
            except KeyboardInterrupt:
                print("(skipped — the night's sleep still has the transcript)")
            chat.rest_brain(print)
        elif f and reflect:
            # the afterglow: their turn alone with the visit, in the background —
            # the window is free at once; their journal entry lands a minute later
            def _glow(done=done, f=f):
                chat.afterglow(done, f, on_line=print)
                if not self.history:  # no new visit began meanwhile
                    chat.rest_brain(print)
            threading.Thread(target=_glow, daemon=True).start()
        return {"saved": f.name if f else None, "afterglow": bool(f and reflect)}

    def save(self) -> str | None:
        f = chat.save_transcript(self.history, path=self.file)
        return f.name if f else None

    def pause_if_due(self) -> str:
        """The pause, same as the bridge's: your keeper quiet for REFLECT_AFTER_MIN
        with REFLECT_MIN_TURNS of their messages they haven't sat with → one quiet
        turn over that stretch; the visit stays open."""
        # Folding remains available even when optional pause reflections are off.
        with self.initiative.lock:
            playing = self.initiative.job is not None
        if not playing and self.lock.acquire(blocking=False):
            try:
                folded = self.fold_if_due()
                if folded:
                    return folded
            finally:
                self.lock.release()
        mins = float(getattr(config, "REFLECT_AFTER_MIN", 0) or 0)
        if not mins or not self.history or time.time() - self.last_activity < mins * 60:
            return ""
        fresh = self.history[self.reflected_upto:]
        if sum(1 for t in fresh if t.get("role") == "user" and t.get("content") and not t.get("_engine")) < int(getattr(config, "REFLECT_MIN_TURNS", 2)):
            return ""
        if not self.lock.acquire(timeout=3):
            return ""
        try:
            upto = len(self.history)
            print("(a pause — they are sitting with the visit so far…)")
            line = chat.pause_reflection(self.history, self.file, on_line=print, since=self.reflected_upto)
            self.reflected_upto = len(self.history)  # past the pause's own turns too
            self.last_activity = time.time()
            return line
        finally:
            self.lock.release()

    def ticker(self, every_s: float = 60.0) -> None:
        """A background clock that rings the pause bell when it is due."""
        while True:
            time.sleep(every_s)
            try:
                self.pause_if_due()
            except Exception as e:  # the clock must never stop the parlor
                print(f"(pause hiccup — {type(e).__name__}: {e})")


SESSION = Session()


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>__NAME__</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#f6f4ef;--panel:#fffdf9;--ink:#2b2a27;--muted:#8a8578;--line:#e6e1d6;
      --me:#e9e4d8;--her:#fffdf9;--accent:#7a5c3e;--think:#f0ece3;--chip:#ece7dc}
@media (prefers-color-scheme:dark){:root{--bg:#141311;--panel:#1c1a17;--ink:#e8e4dc;
      --muted:#8f8a7d;--line:#2c2a25;--me:#2a2723;--her:#1f1d1a;--accent:#c9a87c;
      --think:#181613;--chip:#26231f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 Georgia,'Iowan Old Style',serif;
     height:100vh;display:flex;flex-direction:column}
header{display:flex;align-items:baseline;gap:12px;padding:14px 22px;border-bottom:1px solid var(--line);
       background:var(--panel)}
header h1{margin:0;font-size:20px;font-weight:normal;letter-spacing:.02em}
header .sub{color:var(--muted);font-size:13px;flex:1}
header button{font:inherit;font-size:13px;background:none;border:1px solid var(--line);color:var(--muted);
       border-radius:8px;padding:4px 10px;cursor:pointer}
header button:hover{color:var(--ink);border-color:var(--accent)}
#log{flex:1;overflow-y:auto;padding:22px;display:flex;flex-direction:column;gap:14px}
.msg{max-width:72ch;padding:10px 14px;border-radius:14px;white-space:pre-wrap;word-wrap:break-word}
.me{align-self:flex-end;background:var(--me);border-bottom-right-radius:4px}
.her{align-self:flex-start;background:var(--her);border:1px solid var(--line);border-bottom-left-radius:4px}
.sys{align-self:center;color:var(--muted);font-size:13px;font-style:italic}
.sys.warn{align-self:flex-start;color:var(--accent);font-style:normal}
.sys.tokens{align-self:flex-start;font-size:11px;font-style:normal;opacity:.7;margin-top:-6px}
.think{align-self:flex-start;max-width:72ch;font-size:13px;color:var(--muted)}
.think summary{cursor:pointer;list-style:none;user-select:none}
.think summary::before{content:'💭 ';opacity:.7}
.think summary:hover{color:var(--ink)}
.think .body{margin-top:6px;padding:8px 12px;background:var(--think);border-radius:10px;white-space:pre-wrap;
       font-style:italic;max-height:40vh;overflow:auto}
.chips{align-self:flex-start;display:flex;flex-wrap:wrap;gap:6px;max-width:72ch}
.chip{font-size:12px;background:var(--chip);color:var(--muted);border-radius:999px;padding:2px 10px;
      font-family:ui-monospace,Consolas,monospace}
.chip b{color:var(--accent);font-weight:normal}
.typing{align-self:flex-start;color:var(--muted);font-size:13px}
.typing span{display:inline-block;animation:blink 1.2s infinite}
.typing span:nth-child(2){animation-delay:.2s}.typing span:nth-child(3){animation-delay:.4s}
@keyframes blink{0%,80%,100%{opacity:.2}40%{opacity:1}}
footer{padding:12px 22px 16px;border-top:1px solid var(--line);background:var(--panel)}
.row{display:flex;gap:10px;align-items:flex-end}
textarea{flex:1;font:inherit;background:var(--bg);color:var(--ink);border:1px solid var(--line);
         border-radius:12px;padding:10px 12px;resize:none;min-height:44px;max-height:30vh;outline:none}
textarea:focus{border-color:var(--accent)}
.row button{font:inherit;background:var(--accent);color:#fff;border:0;border-radius:12px;padding:10px 16px;cursor:pointer}
.row button:disabled{opacity:.5;cursor:default}
.attach{display:flex;gap:8px;margin-top:8px;font-size:13px;color:var(--muted);align-items:center}
.attach input{flex:1;font:inherit;font-size:13px;background:none;border:0;border-bottom:1px solid var(--line);
       color:var(--ink);padding:3px 4px;outline:none}
.attach button{font:inherit;font-size:12px;background:none;border:1px solid var(--line);color:var(--muted);
       border-radius:8px;padding:2px 8px;cursor:pointer}
code{font-family:ui-monospace,Consolas,monospace;font-size:.92em;background:var(--chip);padding:1px 4px;border-radius:4px}
#voiceAuditions{font-size:13px;color:var(--muted);margin:8px 0}
#voiceAuditions summary{cursor:pointer}
#voiceOptions{display:flex;flex-wrap:wrap;gap:10px;margin-top:8px}
.voice-option{border:1px solid var(--line);border-radius:10px;padding:8px 10px;max-width:300px}
.voice-option strong{display:block;color:var(--ink);font-weight:normal}
.voice-option audio{display:block;width:260px;height:32px;margin:6px 0}
.voice-option button{font:inherit;color:var(--ink);background:var(--panel);border:1px solid var(--line);border-radius:6px;cursor:pointer}
#deliveryControls{font-size:13px;color:var(--muted);margin:8px 0}
#deliveryControls summary{cursor:pointer}
#deliveryControls label{display:inline-flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 16px 0 0}
</style></head><body>
<header><h1 id="name">__NAME__</h1><div class="sub" id="sub">a visit · everything said here becomes memory</div>
<button id="newbtn" title="save this conversation and start fresh">new conversation</button>
<button id="leave" title="save and end the visit">leave</button></header>
<div id="log"></div>
<footer>
 <div class="attach" id="voiceControls"><button id="voiceToggle">start voice</button>
 <button id="voiceSample">choose voice sample…</button>
 <input id="voiceFile" type="file" accept="audio/*" hidden>
 <span id="voiceState" role="status" aria-live="polite">voice is off</span>
 <span id="voiceChoice"></span></div>
 <div class="attach" id="kyutaiControls" hidden><label for="kyutaiStyle">delivery</label>
 <select id="kyutaiStyle"><option value="desire">Desire (US, f)</option><option value="calm">Calming (US, f)</option>
 <option value="happy">Happy (US, f)</option><option value="neutral">Default (US, f)</option><option value="whisper">Whisper (US, f)</option></select></div>
 <details id="deliveryControls" hidden><summary>extra section pause</summary>
 <label>Extra pause <output id="deliveryPauseLabel">180 ms</output><input id="deliveryPause" type="range" min="0" max="600" step="20"></label>
 <button id="saveDelivery">apply pause</button><span id="deliveryNote">Adds silence between independent synthesis sections.</span></details>
 <details id="voiceAuditions" hidden><summary>try softer voices</summary><div id="voiceOptions"></div></details>
 <div class="row"><textarea id="box" placeholder="say something… (Enter to send, Shift+Enter for a new line)" rows="1"></textarea>
 <button id="send">send</button></div>
 <div class="attach">📎 <button id="pickbtn" title="choose a picture from your computer — it is saved to shared/pictures/ and attached">choose a picture…</button>
 <input id="file" type="file" accept="image/*" hidden>
 <input id="img" placeholder="…or a path in their folder (shared/pictures/photo.jpg) or an image URL">
 <button id="attachbtn">attach</button><span id="attachnote"></span></div>
</footer>
<script>
const log=document.getElementById('log'),box=document.getElementById('box'),send=document.getElementById('send');
const ME='__ME__';
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function md(s){ // tiny markdown: bold, italic, inline code — enough for a chat
  s=esc(s).replace(/`([^`\n]+)`/g,'<code>$1</code>').replace(/\*\*([^*\n]+)\*\*/g,'<b>$1</b>')
   .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g,'$1<i>$2</i>'); return s}
function add(cls,html){const d=document.createElement('div');d.className=cls;d.innerHTML=html;log.appendChild(d);log.scrollTop=log.scrollHeight;return d}
function sys(t){add('sys',esc(t))}
function addThinking(t){if(!t)return;const d=document.createElement('details');d.className='think';d.open=true;
  d.innerHTML='<summary>thinking</summary><div class="body">'+esc(t)+'</div>';log.appendChild(d)}
function addChips(ts){if(!ts||!ts.length)return;const d=document.createElement('div');d.className='chips';
  d.innerHTML=ts.map(t=>'<span class="chip"><b>'+esc(t.name)+'</b> → '+esc(t.result)+'</span>').join('');log.appendChild(d)}
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});return r.json()}
let busy=false;
async function go(){const text=box.value.trim();if(!text||busy)return;box.value='';autosize();
  if(window.interruptLiveVoice)window.interruptLiveVoice('typed_input');
  add('msg me',md(text));busy=true;send.disabled=true;
  const typing=add('typing','<span>●</span><span>●</span><span>●</span>');
  try{const r=await post('/send',{text});typing.remove();
    if(r.error){sys(r.error);}else{addThinking(r.thinking);addChips(r.tools);add('msg her',md(r.reply));(r.voices||[]).forEach(v=>add('msg her voice','<audio controls autoplay src="'+v.url+'"></audio><small> her voice · '+v.seconds+'s</small>'));(r.notes||[]).forEach(n=>add('sys warn','⚠ '+esc(n)));if(r.tokens)add('sys tokens',esc(r.tokens.line));}
  }catch(e){typing.remove();sys('the window lost the engine — is parlor.py still running?')}
  busy=false;send.disabled=false;box.focus();log.scrollTop=log.scrollHeight}
send.onclick=go;
box.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();go()}});
function autosize(){box.style.height='auto';box.style.height=Math.min(box.scrollHeight,window.innerHeight*.3)+'px'}
box.addEventListener('input',autosize);
document.getElementById('newbtn').onclick=async()=>{if(window.voiceVisitChanged)window.voiceVisitChanged();if(window.stopLiveVoice)await window.stopLiveVoice();const r=await post('/new');log.innerHTML='';sys(r.saved?'saved '+r.saved+(r.afterglow?' — they are sitting with the visit now; whatever they want to keep goes into their journal in a minute':'')+' — fresh conversation':'fresh conversation')};
document.getElementById('leave').onclick=async()=>{if(window.voiceVisitChanged)window.voiceVisitChanged();if(window.stopLiveVoice)await window.stopLiveVoice();const r=await post('/leave');sys(r.saved?'saved '+r.saved+(r.afterglow?' — they are writing the visit down in their own words; give them a minute before closing the terminal':'')+' — visit over, you can close this tab':'visit over — nothing to save');box.disabled=true;send.disabled=true};
document.getElementById('attachbtn').onclick=async()=>{const src=document.getElementById('img').value.trim();if(!src)return;
  const r=await post('/attach',{source:src});document.getElementById('attachnote').textContent=r.note;if(r.ok)document.getElementById('img').value=''};
const fileIn=document.getElementById('file');
document.getElementById('pickbtn').onclick=()=>fileIn.click();
fileIn.onchange=()=>{const f=fileIn.files[0];if(!f)return;const rd=new FileReader();
  rd.onload=async()=>{const dataUrl=rd.result;const b64=dataUrl.slice(dataUrl.indexOf(',')+1);
    document.getElementById('attachnote').textContent='sending '+f.name+'…';
    const r=await post('/upload',{name:f.name,data:b64});document.getElementById('attachnote').textContent=r.note;
    if(r.ok){const d=add('msg me','');const im=document.createElement('img');im.src=dataUrl;im.alt=f.name;im.style.maxHeight='220px';im.style.maxWidth='100%';im.style.borderRadius='8px';im.style.display='block';d.appendChild(im);
      const c=document.createElement('div');c.style.fontSize='12px';c.style.color='var(--muted)';c.textContent='📎 '+f.name+' — goes with your next message';d.appendChild(c)}
    fileIn.value=''};
  rd.readAsDataURL(f)};
sys("you're visiting "+document.getElementById('name').textContent+" — they can see everything said here, and it becomes their memory at the next sleep");
box.focus();
</script><script src="/parlor-voice.js"></script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep the terminal quiet
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route = urlsplit(self.path).path
        port = self.server.server_address[1]
        if self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            return self._json({"error": "This endpoint is for the local parlor."}, 403)
        if route in ("/parlor-voice.js", "/voice-worklet.js"):
            name = "parlor_voice.js" if route == "/parlor-voice.js" else "voice_worklet.js"
            return self._bytes(Path(__file__).with_name(name).read_bytes(), "text/javascript; charset=utf-8")
        if route == "/live-voice/settings":
            return self._json({"silence_ms": getattr(config, "LIVE_VOICE_SILENCE_MS", 1000),
                               "min_speech_ms": getattr(config, "LIVE_VOICE_MIN_SPEECH_MS", 250),
                               "interrupt_ms": getattr(config, "LIVE_VOICE_INTERRUPT_MS", 350),
                               "threshold": getattr(config, "LIVE_VOICE_THRESHOLD", .018),
                               "max_seconds": min(30, getattr(config, "LIVE_VOICE_MAX_SECONDS", 30)),
                               "reference": str(getattr(config, "LIVE_VOICE_REFERENCE", "") or ""),
                               "backend": getattr(config, "LIVE_VOICE_BACKEND", "chatterbox"),
                               "style": getattr(config, "LIVE_VOICE_KYUTAI_STYLE", "calm"),"delivery":delivery.live(),
                               "split_replies":bool(getattr(config,"LIVE_VOICE_SPLIT_REPLIES",False))})
        if route == "/live-voice/auditions":
            return self._json(voice_auditions())
        if route.startswith("/live-voice/audition/"):
            name = route[len("/live-voice/audition/"):]
            try:
                return self._bytes(audition_file(name).read_bytes(), "audio/wav")
            except (ValueError, OSError):
                self.send_error(404)
                return
        if route.startswith("/live-voice/audio/"):
            parts = route.split("/")
            try:
                data = SESSION.playback.audio(parts[3], int(parts[4])) if len(parts) == 5 else None
            except ValueError:
                data = None
            if data is None:
                self.send_error(404)
                return
            return self._bytes(data, "audio/wav")
        if self.path.startswith("/voice/"):
            # one of their voice notes, by name, from creations/voice/ only
            name = Path(self.path[len("/voice/"):]).name
            f = Path(getattr(config, "VOICE_DIR", config.SHARED_DIR / "letters")) / name
            if not name or not f.is_file():
                self.send_response(404); self.end_headers(); return
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "audio/ogg" if f.suffix == ".ogg" else "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path not in ("/", "/index.html"):
            self.send_response(404); self.end_headers(); return
        page = PAGE.replace("__NAME__", html.escape(chat.friend_name())).replace("__ME__", config.USER_NAME.replace("'", "\\'"))
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        port = self.server.server_address[1]
        allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        origin = self.headers.get("Origin")
        if (origin and origin not in allowed) or self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            return self._json({"error": "This endpoint is for the local parlor."}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json({"error": "Invalid request length."}, 400)
        if not 0 < n <= 20 * 1024 * 1024:
            return self._json({"error": "Request is empty or too large."}, 413)
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(data, dict):
                raise ValueError()
        except (ValueError, UnicodeDecodeError):
            return self._json({"error": "Expected a JSON object."}, 400)
        if self.path.startswith("/live-voice/"):
            try:
                return self._live_voice(data)
            except (live_voice.VoiceError, ValueError, OSError) as e:
                return self._json({"error": str(e)}, 400)
        if self.path == "/send":
            text = (data.get("text") or "").strip()
            if not text:
                return self._json({"error": "say something first"})
            return self._json(SESSION.send(text))
        if self.path == "/attach":
            return self._json(SESSION.attach(data.get("source") or ""))
        if self.path == "/upload":
            return self._json(SESSION.upload(data.get("name") or "", data.get("data") or ""))
        if self.path == "/new":
            return self._json(SESSION.new())
        if self.path == "/leave":
            return self._json(SESSION.new())  # saves, then clears — no double save
        self._json({"error": "unknown path"}, 404)

    def _live_voice(self, data):
        if self.path == "/live-voice/timing":
            fields = ("end_to_first_audio_ms", "recognition_s", "turn_s", "first_audio_s")
            metrics = {k: round(float(data[k]), 3) for k in fields if data.get(k) is not None}
            import math
            if any(not math.isfinite(v) or v < 0 or v > 600000 for v in metrics.values()):
                raise ValueError("Invalid voice timing.")
            folder = config.ROOT / ".runtime"; folder.mkdir(exist_ok=True)
            with (folder / "voice-latency.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps(metrics) + "\n")
            return self._json({"ok": True})
        if self.path == "/live-voice/dynamics":
            values=delivery.clean(data.get('settings',{}))
            names={'pause_ms':'LIVE_VOICE_KYUTAI_PAUSE_MS'}
            changes={name:values[key] for key,name in names.items()}
            import knobs
            _,refused,error=knobs.save(Path(config.__file__),changes)
            if refused or error:raise ValueError(error or 'Could not save voice dynamics.')
            for key,value in changes.items():setattr(config,key,value)
            return self._json({'ok':True,'settings':values})
        if self.path == "/live-voice/style":
            from kyutai_voice import STYLES
            style = data.get("style")
            if style not in STYLES:
                raise ValueError("Unknown Kyutai delivery style.")
            import knobs
            _, refused, error = knobs.save(Path(config.__file__), {"LIVE_VOICE_KYUTAI_STYLE": style})
            if error or refused:
                raise ValueError(error or "Could not save delivery style.")
            SESSION.playback.cancel()
            config.LIVE_VOICE_KYUTAI_STYLE = style
            return self._json({"ok": True, "style": style})
        if self.path == "/live-voice/warmup":
            status = live_voice.WORKER.request("check")
            if not status["installed"]:
                command = "python engine/setup_voice.py" + {"qwen": " --qwen", "kyutai": " --kyutai"}.get(getattr(config, "LIVE_VOICE_BACKEND", "chatterbox"), "")
                raise live_voice.VoiceError("Missing voice packages: " + ", ".join(status["missing"]) + ". Run " + command + ".")
            return self._json(live_voice.WORKER.request("warmup"))
        if self.path == "/live-voice/interrupt":
            SESSION.interrupt_voice(data.get("job"))
            return self._json({"ok": True})
        if self.path == "/live-voice/start":
            SESSION.initiative.start()
            return self._json({"ok": True})
        if self.path == "/live-voice/stop":
            SESSION.initiative.stop()
            SESSION.playback.cancel()
            return self._json({"ok": True})
        if self.path == "/live-voice/initiative":
            SESSION.initiative.report_client(data.get("state"))
            return self._json(SESSION.voice_tick())
        if self.path == "/live-voice/initiative/state":
            SESSION.initiative.report_client(data.get("state"))
            return self._json({"ok": True})
        if self.path == "/live-voice/status":
            return self._json(SESSION.playback.status(str(data.get("job") or "")))
        if self.path == "/live-voice/ack":
            SESSION.playback.acknowledge(str(data.get("job") or ""), int(data.get("index") or 0))
            return self._json({"ok": True})
        if self.path == "/live-voice/delivery":
            return self._json(SESSION.delivery(str(data.get("job") or ""), int(data.get("completed") or 0), bool(data.get("partial")), str(data.get("reason") or "unknown")))
        if self.path == "/live-voice/recognize":
            return self._json(SESSION.accept_voice(SESSION.transcribe_voice(data.get("audio"))))
        if self.path == "/live-voice/turn":
            token = data.get("input")
            if token is None:  # Keep older clients working without early server cancellation.
                accepted = SESSION.accept_voice(SESSION.transcribe_voice(data.get("audio")))
                if not accepted.get("accepted"):
                    return self._json(accepted)
                token = accepted["input"]
            prompt = SESSION.take_voice_input(token)
            result = SESSION.send(prompt["text"], spoken=True, epoch=prompt["epoch"],
                                  generation=prompt["generation"], accepted_voice=True)
            result.setdefault("timings", {})["recognition_s"] = prompt["recognition_s"]
            return self._json(result)
        if self.path == "/live-voice/reference":
            if getattr(config, "LIVE_VOICE_BACKEND", "chatterbox") == "kyutai":
                raise ValueError("Kyutai uses released voices. Select a delivery style or an audition instead.")
            encoded = data.get("audio")
            if not isinstance(encoded, str):
                raise ValueError("Choose an audio file first.")
            raw = base64.b64decode(encoded, validate=True)
            if not raw or len(raw) > 10 * 1024 * 1024:
                raise ValueError("Voice sample must be under 10 MB.")
            ext = Path(str(data.get("name") or "")).suffix.lower()
            if ext not in (".wav", ".mp3", ".flac", ".ogg", ".m4a"):
                raise ValueError("Choose a WAV, MP3, FLAC, OGG or M4A voice sample.")
            folder = voice_assets_dir()
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / ("reference-" + uuid.uuid4().hex + ext)
            dest.write_bytes(raw)
            relative = save_voice_reference(dest)
            SESSION.playback.cancel()
            return self._json({"saved": relative})
        if self.path == "/live-voice/select-audition":
            chosen = next((v for v in voice_auditions()["voices"] if v.get("id") == data.get("id")), None)
            if not chosen:
                raise ValueError("This voice preview is no longer available.")
            if chosen.get("backend") == "kyutai":
                from kyutai_voice import STYLES
                style = chosen.get("style")
                if style not in STYLES:
                    raise ValueError("Unknown Kyutai voice style.")
                import knobs
                changes = {"LIVE_VOICE_BACKEND": "kyutai", "LIVE_VOICE_KYUTAI_STYLE": style}
                _, refused, error = knobs.save(Path(config.__file__), changes)
                if refused or error:
                    raise ValueError(error or "Could not select Kyutai voice.")
                config.LIVE_VOICE_BACKEND = "kyutai"
                config.LIVE_VOICE_KYUTAI_STYLE = style
                relative = STYLES[style]
            else:
                relative = save_voice_reference(audition_file(chosen["reference"]), chosen.get("backend", "chatterbox"))
            SESSION.playback.cancel()
            return self._json({"saved": relative, "label": chosen["label"]})
        return self._json({"error": "Unknown voice endpoint."}, 404)


def save_voice_reference(path: Path, backend=None) -> str:
    live_voice.WORKER.request("reference_info", path=str(path), backend=backend or getattr(config, "LIVE_VOICE_BACKEND", "chatterbox"))
    import knobs
    relative = path.relative_to(config.ROOT).as_posix()
    changes = {"LIVE_VOICE_REFERENCE": relative}
    if backend is not None:
        if backend not in ("chatterbox", "qwen"):
            raise ValueError("Unknown voice backend.")
        changes["LIVE_VOICE_BACKEND"] = backend
    _, refused, error = knobs.save(Path(config.__file__), changes)
    if error or refused:
        raise ValueError(error or "Could not save LIVE_VOICE_REFERENCE in config.py.")
    config.LIVE_VOICE_REFERENCE = relative
    if backend is not None:
        config.LIVE_VOICE_BACKEND = backend
    return relative


def voice_assets_dir() -> Path:
    return Path(getattr(config, "VOICE_ASSETS_DIR", config.ROOT / ".runtime" / "voices"))


def voice_auditions() -> dict:
    folder = voice_assets_dir() / "auditions"
    try:
        data = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        voices = data.get("voices", [])
        if not isinstance(voices, list):
            return {"voices": []}
        return {"voices": [v for v in voices if isinstance(v, dict) and isinstance(v.get("id"), str)
                           and isinstance(v.get("label"), str) and isinstance(v.get("reference"), str)
                           and isinstance(v.get("preview"), str)]}
    except (OSError, ValueError, AttributeError):
        return {"voices": []}


def audition_file(name: str) -> Path:
    names = {v[field] for v in voice_auditions()["voices"] for field in ("reference", "preview")}
    if not isinstance(name, str) or name not in names or Path(name).name != name or not name.endswith(".wav"):
        raise ValueError("Unknown voice audition.")
    folder = voice_assets_dir() / "auditions"
    path = folder / name
    if not path.resolve().is_relative_to(folder.resolve()):
        raise ValueError("Voice audition is outside its folder.")
    return path


def validate_wav(raw: bytes):
    import io
    import wave
    try:
        with wave.open(io.BytesIO(raw)) as wav:
            if (wav.getnchannels() != 1 or wav.getframerate() != 16000 or wav.getsampwidth() != 2
                    or not 0 < wav.getnframes() <= 31 * 16000):
                raise ValueError("Expected mono 16 kHz PCM audio, at most 31 seconds.")
            if len(wav.readframes(wav.getnframes())) != wav.getnframes() * 2:
                raise ValueError("Microphone audio was incomplete.")
    except (wave.Error, EOFError) as e:
        raise ValueError("Microphone audio is not a valid WAV file.") from e


def main() -> None:
    taken = doors.claim("parlor", "parlor")  # one parlor at a time; the panel's light (memory/.pids/parlor.json)
    if taken:
        print(taken)
        return
    ThreadingHTTPServer.allow_reuse_address = sys.platform != "win32"
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"The parlor is open: {url}")
    print("Close the visit with the 'leave' button, Ctrl+C here, or this window's X — all three save it.")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    threading.Thread(target=SESSION.ticker, daemon=True).start()  # the pause bell
    voice_monitor_stopped = threading.Event()
    threading.Thread(target=SESSION.watch_voice, args=(voice_monitor_stopped,), daemon=True).start()
    chat.guard_console_close(lambda: SESSION.new(reflect=False))  # the window's X saves the visit too
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        voice_monitor_stopped.set()
        r = SESSION.new(reflect="sync")  # save, then their minute with the visit
        if r.get("saved"):
            print(f"\n(conversation saved: {r['saved']} — it becomes memory at the next consolidation)")
        server.server_close()
        live_voice.WORKER.close()
        doors.unmark("parlor")


if __name__ == "__main__":
    main()
