"""A silence clock and a private, context-aware choice to speak or wait.

The clock never writes dialogue. Front ends tick it only while listening;
confirmed playback completion arms it. A model decision has no external
tools and is committed only if no input, interruption or stop superseded it.
"""
from __future__ import annotations

import atexit
import json
import math
import queue
import random
import sys
import threading
import time

import config


_CONSOLE_LOCK = threading.Lock()
_CONSOLE_QUEUE = queue.Queue(maxsize=256)
_CONSOLE_THREAD = None
_CONSOLE_DROPPED = 0
_CONSOLE_CLOSING = False


def _write_console(stream, output):
    try:
        print(output, file=stream, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        try:
            print(output.encode(encoding, "backslashreplace").decode(encoding), file=stream, flush=True)
        except (OSError, ValueError):
            pass
    except (OSError, ValueError):
        pass


def _console_writer():
    global _CONSOLE_DROPPED
    while True:
        stream, output, done = _CONSOLE_QUEUE.get()
        if stream is None and output is None and done is None:
            _CONSOLE_QUEUE.task_done()
            return
        try:
            if output is not None:
                with _CONSOLE_LOCK:
                    dropped, _CONSOLE_DROPPED = _CONSOLE_DROPPED, 0
                if dropped:
                    _write_console(stream, f"[voice/log] {dropped} log entries dropped while console output was blocked.")
                _write_console(stream, output)
        finally:
            if done:
                done.set()
            _CONSOLE_QUEUE.task_done()


def flush_console(timeout=1.0):
    """Bounded drain for tests/shutdown, never used by a voice timing path."""
    if _CONSOLE_THREAD is None:
        return True
    done = threading.Event()
    try:
        _CONSOLE_QUEUE.put_nowait((None, None, done))
    except queue.Full:
        return False
    return done.wait(timeout)


def shutdown_console():
    """Finish queued output before Python finalizes its buffered streams."""
    global _CONSOLE_CLOSING
    with _CONSOLE_LOCK:
        _CONSOLE_CLOSING = True
    thread = _CONSOLE_THREAD
    if thread is None:
        return
    flush_console()
    try:
        _CONSOLE_QUEUE.put_nowait((None, None, None))
    except queue.Full:
        return
    thread.join(timeout=1.0)


atexit.register(shutdown_console)


def console_log(source, message, *, reason="", thinking="", text=""):
    """Debug output in the owning server's console; never dialogue or a file."""
    if not getattr(config, "LIVE_VOICE_INITIATIVE_LOG", True):
        return
    label = {"voice": "parlor", "phone_voice": "phone", "discord_voice": "discord"}.get(source, source)
    prefix = f"[{time.strftime('%H:%M:%S')}] [voice/{label}] "
    lines = [prefix + message]
    if reason:
        lines.append(prefix + "Reason: " + json.dumps(reason, ensure_ascii=False))
    if text:
        lines.append(prefix + "Proposed speech: " + json.dumps(text, ensure_ascii=False))
    if thinking and getattr(config, "LIVE_VOICE_INITIATIVE_LOG_THINKING", False):
        lines.append(prefix + "Reasoning returned by model (proposed choice):")
        lines.extend(prefix + "  " + line for line in thinking.splitlines())
    output = "\n".join(lines)
    # A console selection or slow output can block Windows writes. Queue the
    # text without waiting; the clock/model never waits on the console sink.
    global _CONSOLE_THREAD, _CONSOLE_DROPPED
    with _CONSOLE_LOCK:
        if _CONSOLE_CLOSING or sys.is_finalizing():
            return
        if _CONSOLE_THREAD is None or not _CONSOLE_THREAD.is_alive():
            _CONSOLE_THREAD = threading.Thread(target=_console_writer, name="voice-console", daemon=True)
            _CONSOLE_THREAD.start()
        try:
            _CONSOLE_QUEUE.put_nowait((sys.stdout, output, None))
        except queue.Full:
            _CONSOLE_DROPPED += 1


CLIENT_BLOCKERS = {
    "checking": "a private decision request is already pending on the client",
    "busy": "the client is waiting for an accepted prompt's reply",
    "recognizing": "microphone audio is queued or being recognized",
    "recovering": "the microphone is reconnecting",
    "preview": "a voice preview is active",
    "capture": "an unfinished microphone capture is active (not an accepted prompt)",
    "candidate": "candidate sound is being collected (not an accepted prompt)",
    "queued": "unverified microphone captures are queued",
    "playback": "the client still tracks a playback job",
    "scheduled_audio": "audio buffers are still scheduled on the client",
    "echo_tail": "the playback echo-tail guard is active",
    "microphone_stale": "microphone frames have not reached the client for over 3s",
    "output_unavailable": "the browser audio output is unavailable or suspended",
}


def bounded_seconds(value, default=60.0):
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(3.0, min(600.0, seconds)) if math.isfinite(seconds) else default


class Clock:
    def __init__(self, now=time.monotonic, gaussian=random.gauss, source="voice"):
        self.now, self.gaussian = now, gaussian
        self.source = source
        self.scheduler = "client"
        self.lock = threading.RLock()
        self.enabled = False
        self.version = 0
        self.inflight = None
        self.recognizing = 0
        self.last_user = self.last_spoken = self.due = None
        self.job = None
        self.speaking_at = None
        self.next_delay = None
        self.followups = 0
        self.wait_for_user = False
        self.checks = 0
        self.inflight_at = None
        self.inflight_check = self.inflight_version = None
        self.client_seen = self.last_request = None
        self.client_blockers = ()
        self.client_details = {}
        self.deadline_logged = False
        self.monitor_at = None
        self.monitor_key = None

    def _delay(self):
        mean = bounded_seconds(getattr(config, "LIVE_VOICE_INITIATIVE_MEAN_S", 25), 25)
        try:
            spread = float(getattr(config, "LIVE_VOICE_INITIATIVE_STD_S", 6))
            spread = max(0.0, min(90.0, spread)) if math.isfinite(spread) else 6.0
        except (TypeError, ValueError, OverflowError):
            spread = 6.0
        return max(5.0, min(90.0, self.gaussian(mean, spread)))

    def start(self, source=None, scheduler=None):
        with self.lock:
            if source:
                self.source = source
            if scheduler:
                self.scheduler = scheduler
            self.version += 1
            self.enabled = True
            self.last_user = self.last_spoken = self.due = None
            self.job = self.next_delay = None
            self.followups = 0
            self.wait_for_user = False
            self.checks = 0
            self.client_seen = self.last_request = None
            self.client_blockers = ()
            self.client_details = {}
            self.deadline_logged = False
            self.monitor_at = self.monitor_key = None
            active = bool(getattr(config, "LIVE_VOICE_INITIATIVE", True))
            think = bool(getattr(config, "LIVE_VOICE_INITIATIVE_THINK", True))
            mean = getattr(config, "LIVE_VOICE_INITIATIVE_MEAN_S", 25)
            spread = getattr(config, "LIVE_VOICE_INITIATIVE_STD_S", 6)
            console_log(self.source, f"Voice started; initiative {'enabled' if active else 'disabled'}, "
                        f"thinking {'on' if think else 'off'}, scheduler {self.scheduler}, first pause mean {mean}s / standard deviation {spread}s. "
                        "Waiting for a completed spoken reply.")

    def stop(self):
        with self.lock:
            if self.enabled:
                console_log(self.source, "Voice stopped; automatic checks disabled.")
            self.enabled = False
            self.interrupt("voice stopped")

    def interrupt(self, reason="interruption"):
        with self.lock:
            if self.enabled and (self.due is not None or self.job or self.inflight):
                console_log(self.source, f"Silence check cancelled or paused: {reason}.")
            self.version += 1
            self.due = self.job = None

    def heard(self):
        with self.lock:
            changed = self.last_user is None or self.wait_for_user or self.followups or self.due is not None or self.job
            self.interrupt("new input")
            self.last_user = self.now()
            self.followups = 0
            self.next_delay = None
            self.wait_for_user = False
            if self.enabled and changed:
                console_log(self.source, "New input received; follow-up count reset. Waiting for reply playback.")

    def begin_recognition(self):
        with self.lock:
            self.recognizing += 1

    def end_recognition(self):
        with self.lock:
            self.recognizing = max(0, self.recognizing - 1)

    def speaking(self, job):
        with self.lock:
            self.due = None
            self.job = job
            self.speaking_at = self.now() if job else None
            if self.enabled:
                console_log(self.source, "Speech queued; silence timer waits for playback completion."
                            if job else "Speech was not queued; no playback completion to wait for.")

    def finished(self, job, interrupted=False):
        with self.lock:
            if not job or job != self.job:
                return
            self.job = None
            if interrupted:
                console_log(self.source, "Playback interrupted; automatic checks pause until new input.")
                self.interrupt("playback interrupted")
                return
            self.last_spoken = self.now()
            self._arm("Playback completed")

    def _arm(self, trigger="Decision accepted"):
        if self.enabled and self.last_user is not None and not self.wait_for_user:
            delay = self.next_delay if self.next_delay is not None else self._delay()
            self.due = self.now() + delay
            self.deadline_logged = False
            self.monitor_at = self.monitor_key = None
            console_log(self.source, f"{trigger}; next check in {delay:.1f}s.")
        elif self.enabled and self.wait_for_user:
            console_log(self.source, f"{trigger}; waiting for your next words, with no scheduled check.")

    def empty_capture(self):
        # Acoustic activity was never an interrupt. Rejection must preserve
        # a pending choice, the existing deadline and any playing reply.
        with self.lock:
            if self.enabled:
                console_log(self.source, "Microphone audio rejected: no words detected. Playback and silence timer unchanged.")

    def report_client(self, state):
        """Read-only diagnostics. Unverified sound never invalidates a choice."""
        if not isinstance(state, dict):
            return
        with self.lock:
            codes = state.get("blocked", [])
            if not isinstance(codes, list):
                codes = []
            self.client_seen = self.now()
            self.client_blockers = tuple(code for code in CLIENT_BLOCKERS if code in codes)
            self.client_details = {}
            phase = state.get("phase")
            if phase in ("off", "warming", "thinking", "speaking", "listening"):
                self.client_details["phase"] = phase
            for key in ("mic_age_ms", "queued_turns", "scheduled_audio", "candidate_ms", "echo_tail_ms"):
                value = state.get(key)
                if isinstance(value, (int, float)) and math.isfinite(value):
                    self.client_details[key] = round(max(0, min(1e9, value)), 1)

    def request_received(self):
        with self.lock:
            self.last_request = self.now()

    def ready(self):
        with self.lock:
            return (self.enabled and bool(getattr(config, "LIVE_VOICE_INITIATIVE", True))
                    and self.inflight is None and not self.job and self.due is not None and self.now() >= self.due)

    def _deadline(self):
        if self.due is not None and self.now() >= self.due and not self.deadline_logged:
            self.deadline_logged = True
            console_log(self.source, f"Check deadline reached; overdue by {max(0, self.now() - self.due):.1f}s. "
                        "A check must start or report why it is deferred.")

    def monitor(self, server_busy=False, playback_status=None):
        """Independent server heartbeat: report deadlines even without client polling."""
        with self.lock:
            if not self.enabled or not getattr(config, "LIVE_VOICE_INITIATIVE_LOG", True):
                return
            now = self.now()
            self._deadline()
            if self.inflight is not None:
                elapsed = now - self.inflight_at if self.inflight_at is not None else 0
                if elapsed < 10:
                    return
                superseded = self.inflight_version != self.version
                key = ("running", self.inflight_check, superseded)
                message = f"Check {self.inflight_check} still running after {elapsed:.1f}s; waiting for the model response."
                if superseded:
                    message += " This request was superseded and its result will be discarded."
            elif self.job:
                elapsed = now - self.speaking_at if self.speaking_at is not None else 0
                if elapsed < 10:
                    return
                status = playback_status if playback_status and playback_status.get("_job_id") == self.job else {}
                key = ("playback", self.job, bool(status.get("done")), bool(status.get("error")))
                message = f"Waiting for playback completion after {elapsed:.1f}s; the silence clock is not armed yet."
                if status:
                    stage = "audio generation finished; awaiting client delivery receipt" if status.get("done") else "audio generation is still pending"
                    message += f" {stage}; {len(status.get('chunks', []))} audio segments ready."
                    if status.get("error"):
                        message += " Voice error: " + json.dumps(status["error"], ensure_ascii=False)
            elif self.due is not None and now >= self.due:
                self._deadline()
                age = None if self.client_seen is None else now - self.client_seen
                stale = age is None or age > 3
                key = ("deferred", server_busy, stale if self.scheduler != "server" else None,
                       self.client_blockers if self.scheduler != "server" else ())
                reasons = []
                if not getattr(config, "LIVE_VOICE_INITIATIVE", True):
                    reasons.append("initiative is disabled in configuration")
                if server_busy:
                    reasons.append("the server conversation lock is busy")
                if self.scheduler == "server":
                    if not reasons:
                        reasons.append("waiting for the PC scheduler to start the check")
                elif age is None:
                    reasons.append("no client status has been received")
                elif stale:
                    reasons.append(f"client status is {age:.1f}s old; current client readiness is unknown")
                if self.scheduler != "server":
                    reasons.extend(("last reported: " if stale else "") + CLIENT_BLOCKERS[code] for code in self.client_blockers)
                if not reasons:
                    reasons.append("waiting for an eligible client check request")
                request_age = "none yet" if self.last_request is None else f"{now - self.last_request:.1f}s ago"
                actor = "PC scheduling attempt" if self.scheduler == "server" else "check request"
                message = (f"Check deferred; overdue {now - self.due:.1f}s: " + "; ".join(reasons)
                           + f". Last {actor}: {request_age}. Last reported state: "
                           + json.dumps(self.client_details, ensure_ascii=False))
            else:
                return
            try:
                interval = float(getattr(config, "LIVE_VOICE_INITIATIVE_LOG_INTERVAL_S", 10))
                interval = max(1, min(600, interval)) if math.isfinite(interval) else 10
            except (TypeError, ValueError, OverflowError):
                interval = 10
            if key != self.monitor_key or self.monitor_at is None or now - self.monitor_at >= interval:
                self.monitor_key, self.monitor_at = key, now
                console_log(self.source, message)

    def watch(self, stopped):
        while not stopped.wait(1):
            self.monitor()

    def claim(self):
        with self.lock:
            if (not self.enabled or not getattr(config, "LIVE_VOICE_INITIATIVE", True)
                    or self.recognizing or self.inflight is not None or self.job or self.due is None
                    or self.now() < self.due):
                return None
            self._deadline()
            self.inflight = ticket = object()
            self.inflight_at = self.now()
            overdue = self.now() - self.due
            self.due = None
            self.checks += 1
            self.inflight_check, self.inflight_version = self.checks, self.version
            elapsed = {
                "since_user_s": round(self.now() - self.last_user, 1),
                "since_spoken_s": round(self.now() - self.last_spoken, 1),
                "followups": self.followups,
            }
            console_log(self.source, f"Check {self.checks} started: last speech finished {elapsed['since_spoken_s']:g}s ago; "
                        f"your last words {elapsed['since_user_s']:g}s ago; follow-ups {self.followups}; "
                        f"started {overdue:.1f}s after its deadline; "
                        f"thinking {'on' if getattr(config, 'LIVE_VOICE_INITIATIVE_THINK', True) else 'off'}.")
            return ticket, self.version, elapsed

    def resolve(self, claim, decision, commit):
        with self.lock:
            ticket, version, _ = claim
            if self.inflight is not ticket:
                return False
            self.inflight = None
            if not self.enabled or version != self.version or not getattr(config, "LIVE_VOICE_INITIATIVE", True):
                console_log(self.source, "Decision discarded; input, interruption, voice stop or a settings change superseded it.")
                return False
            if self.recognizing:
                # Input is being checked, not yet accepted. Preserve existing
                # playback but do not commit a new autonomous line over it.
                if not self.job:
                    self._arm()
                console_log(self.source, "Decision deferred; microphone recognition is pending.")
                return False
            self.next_delay = bounded_seconds(decision.get("next_check_seconds"))
            self.wait_for_user = bool(decision.get("wait_for_user"))
            if decision.get("text"):
                self.followups += 1
            commit()
            if not self.job:
                self._arm()
            else:
                pause = "then wait for your next words" if self.wait_for_user else f"next check {self.next_delay:g}s after playback"
                console_log(self.source, f"Decision accepted: speak; {pause}.")
            return True


CHOICE = [{"type": "function", "function": {
    "name": "voice_initiative",
    "description": "Only for an engine quiet-moment event: privately choose speech or silence and the next pause. For actual user messages, answer normally instead of calling this tool.",
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["speak", "wait"]},
        "text": {"type": "string", "description": "Only the words to say aloud, or empty when waiting."},
        "reason": {"type": "string", "description": "A brief private explanation of this choice for console debugging; never spoken."},
        "next_check_seconds": {"type": "number", "description": "Seconds of silence before considering speaking again (3 to 600), measured after playback if speaking."},
        "wait_for_user": {"type": "boolean", "description": "True to remain quiet until the person speaks again, instead of scheduling another check."},
    }, "required": ["action", "text", "next_check_seconds", "wait_for_user"]},
}}]


def voice_tools():
    import tools
    # Tool schemas participate in the model's prompt prefix. Keep the same
    # list on human voice turns and quiet checks; the latter never dispatch
    # external tools, even though their schemas remain in the cached prefix.
    definitions = tools.current_definitions()
    return definitions + CHOICE if getattr(config, "LIVE_VOICE_INITIATIVE", True) else definitions


def decide(history, elapsed, mode="voice", physicalize=False, physicalize_voice=None):
    """Return a choice and new history turns; never mutate the live history."""
    import assemble
    import chat
    import ollama_client

    latest_words = next((t.get("content", "") for t in reversed(history)
                         if t.get("role") == "user" and not t.get("_engine")), "")
    # Keep the current invitation near the end of even a long narration.
    # It remains a quotation of the existing message, never a new user turn.
    invitation = json.dumps(latest_words[:4000], ensure_ascii=False)
    bell = {"role": "user", "_engine": True, "_voice_mode": True,
            "_voice_backend": getattr(config, "LIVE_VOICE_BACKEND", "chatterbox"),
            "_voice_phone": mode == "phone_voice", "_voice_discord": mode == "discord_voice",
            "content": (
                "[engine: a quiet moment in the ongoing voice visit, not a message from the person. "
                f"It has been {elapsed['since_spoken_s']:g} seconds since your last speech finished playing, "
                f"and {elapsed['since_user_s']:g} seconds since their last words were received. "
                f"You have spoken {elapsed['followups']} additional turns without new words from them. "
                f"Their most recent actual words, quoted for context rather than a new message: {invitation}. "
                "Consider the whole conversation and choose freely whether to speak or wait. "
                "Silence alone does not tell you whether they are asleep, absent, or listening. "
                "An invitation to tell a story, keep talking, or talk them to sleep can span many spoken "
                "passages and pauses without requiring replies. Use the elapsed time and any duration "
                "they requested to decide how to continue and when to finish. Respect requests for quiet "
                "or a pause; you may wait for them indefinitely. In ordinary conversation, give them "
                "space to answer or choose silence rather than repeatedly checking on them. "
                "Use voice_initiative exactly once; do not call any other tools in this private check. "
                "Its action and timing are private control decisions; "
                "only its text when action=speak is spoken. Write that text in your own voice, continuing "
                "the context naturally, without quoting this event, inventing a reply from them, or "
                "explaining the timer. You need not end each passage with a question.]")}
    if physicalize:
        bell["_physicalize"] = True
        if physicalize_voice is not None:
            bell["_physicalize_voice"] = dict(physicalize_voice)
    device = assemble.handy_section()
    if device:
        bell["content"] += "\n\n" + device
    if getattr(config, "LIVE_VOICE_INITIATIVE_LOG", True):
        bell["content"] += "\n\n[engine: Include a short reason for your choice in the private reason field, for console debugging. It is never spoken.]"
    system = history[0].get("_system") if history else None
    if not system:
        system = assemble.system_prompt(latest_words, mode=mode, warm=True, **({"physicalize": True} if physicalize else {}))
    messages = [{"role": "system", "content": system}] + [chat.render_turn(t) for t in history + [bell]]
    started = time.monotonic()
    msg = ollama_client.chat(messages, tools=voice_tools(), expect_words=False,
                            think=bool(getattr(config, "LIVE_VOICE_INITIATIVE_THINK", True)), think_retries=0)
    duration = time.monotonic() - started
    tokens = msg.get("tokens") or {}
    if tokens:
        console_log(mode, f"Model timings: loading {tokens.get('load_s', 0):.2f}s; "
                    f"prompt reading {tokens.get('prompt_s', 0):.2f}s; generation {tokens.get('reply_s', 0):.2f}s; "
                    f"prompt {tokens.get('prompt', 0)} tokens; generated {tokens.get('reply', 0)} tokens.")
    if getattr(config, "LIVE_VOICE_INITIATIVE_LOG_THINKING", False) and not msg.get("thinking"):
        status = "thinking is disabled" if not getattr(config, "LIVE_VOICE_INITIATIVE_THINK", True) else "no reasoning trace was returned"
        console_log(mode, f"Separate reasoning: none ({status}); the private decision/reason is logged below.")
    calls = msg.get("tool_calls") or []
    fallback = {"text": "", "next_check_seconds": 60, "wait_for_user": False}

    def invalid(detail):
        console_log(mode, f"Invalid model choice after {duration:.2f}s: {detail}; fallback is quiet, retry in 60s.",
                    thinking=msg.get("thinking") or "")
        console_log(mode, "Rejected model output (never spoken): " + json.dumps(
            {"content": msg.get("content"), "tool_calls": calls}, ensure_ascii=False))
        return fallback, []

    if len(calls) != 1 or calls[0].get("function", {}).get("name") != "voice_initiative":
        return invalid("expected exactly one voice_initiative call")
    args = calls[0]["function"].get("arguments", {})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            return invalid("could not parse the decision arguments")
    if (not isinstance(args, dict) or args.get("action") not in ("wait", "speak")
            or not isinstance(args.get("text"), str) or not isinstance(args.get("wait_for_user"), bool)):
        return invalid("missing or invalid action, text or wait_for_user fields")
    text = chat.strip_voice_delivery(ollama_client.defang(args["text"])[0]).strip() if args["action"] == "speak" else ""
    result = {"text": text, "next_check_seconds": bounded_seconds(args.get("next_check_seconds")),
              "wait_for_user": args["wait_for_user"]}
    reason = args.get("reason") if isinstance(args.get("reason"), str) else ""
    pause = "wait until you speak" if result["wait_for_user"] else f"next pause {result['next_check_seconds']:g}s"
    action = "SPEAK" if text else "WAIT"
    console_log(mode, f"Model proposed {action} after {duration:.2f}s; {pause}; "
                f"requested interval {json.dumps(args.get('next_check_seconds'))}, "
                f"effective interval {result['next_check_seconds']:g}s; wait_for_user={result['wait_for_user']}.",
                reason=reason or "(model did not provide a private reason)",
                thinking=msg.get("thinking") or "", text=text)
    # Keep the model's actual control exchange for the warm prefix, hidden
    # from transcripts, memory retrieval, user-message counts and playback.
    additions = [bell]
    additions.extend(dict(t, _engine=True) for t in msg.get("sent_extra", []))
    additions.extend([dict(msg, _engine=True), {"role": "tool", "tool_name": "voice_initiative", "_engine": True,
        "content": "[engine: your private choice was accepted; only the chosen spoken text is delivered.]"}])
    if text:
        additions.append({"role": "assistant", "content": text, "_initiative": True})
    return result, additions
