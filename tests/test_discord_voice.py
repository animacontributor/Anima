"""Calls and ownership contracts, with no Discord, models or personal file writes."""
import base64
import io
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))
import assemble
import chat
import config
import discord_bridge
import discord_voice
import doors
import live_voice
import ollama_client
import voice_lock
import voice_worker


def audio(rate=48000, channels=2, frames=960):
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setframerate(rate); w.setnchannels(channels); w.setsampwidth(2)
        w.writeframes(b"\0\0" * frames * channels)
    return base64.b64encode(b.getvalue()).decode()


def bridge():
    return SimpleNamespace(history=[], attached=[], lock=threading.RLock(),
        _checkpoint=Mock(), _try_send=Mock(), _send_reply=Mock(),
        _carry_voice=Mock(), fold_if_due=Mock(), show_tools=False, last_activity=0)


class CaptureTests(unittest.TestCase):
    def test_validates_discord_format_and_length(self):
        a = audio()
        self.assertEqual(discord_voice.validate_capture(a), a)
        for invalid in (audio(rate=16000, channels=1), audio(frames=0), audio(frames=32 * 48000), "garbage"):
            with self.assertRaises((ValueError, wave.Error)):
                discord_voice.validate_capture(invalid)

    def test_default_channel_settings_stay_local(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            path.write_text('{"channel_id": "100000000000000123"}')
            with patch.object(discord_voice, "SETTINGS_FILE", path), patch.object(config, "DISCORD_VOICE_CHANNEL_ID", ""):
                self.assertEqual(discord_voice.default_channel(), "100000000000000123")
            with patch.object(config, "DISCORD_VOICE_CHANNEL_ID", "100000000000000456"):
                self.assertEqual(discord_voice.default_channel(), "100000000000000456")

    @unittest.skipUnless(all(importlib.util.find_spec(n) for n in ("av", "numpy", "soundfile")), "audio packages unavailable")
    def test_real_resampler_preserves_time_and_filters_aliasing_without_changing_decoder(self):
        import numpy as np
        import soundfile as sf
        rate = 48000
        t = np.arange(rate, dtype=np.float32) / rate
        tone = .4 * np.sin(2 * np.pi * 500 * t) + .2 * np.sin(2 * np.pi * 20000 * t)
        b = io.BytesIO()
        sf.write(b, np.column_stack([tone, tone]), rate, format="WAV", subtype="PCM_16")
        recognizer = Mock()
        recognizer.transcribe.return_value = ([SimpleNamespace(text="Squadron 42")], None)
        with patch.object(voice_worker, "models"), patch.object(voice_worker, "_stt", recognizer):
            result = voice_worker._handle({"op": "transcribe", "audio": base64.b64encode(b.getvalue()).decode(),
                "discord_input": True, "stt_beam_size": 5, "language": "en", "vocab": "Squadron 42"})
        samples = recognizer.transcribe.call_args.args[0]
        self.assertEqual(samples.shape, (16000,))
        spectrum = abs(np.fft.rfft(samples))
        self.assertLess(spectrum[4000] / spectrum[500], .01)
        self.assertAlmostEqual(float(np.max(samples[100:-100])), .4, delta=.01)
        kwargs = recognizer.transcribe.call_args.kwargs
        self.assertEqual(kwargs["beam_size"], 5)
        self.assertEqual(kwargs["initial_prompt"], "Squadron 42")
        self.assertTrue(kwargs["vad_filter"])
        self.assertEqual(result["text"], "Squadron 42")


class TurnTests(unittest.TestCase):
    def setUp(self):
        self.bridge = bridge()
        self.worker = Mock()
        self.call = discord_voice.Call(self.bridge, self.worker)
        self.call.active = self.call.connected = True
        self.sent = []

        def send(msg):
            self.sent.append(msg)
            if msg["op"] == "audio":
                self.call.acked.add((msg["job"], msg["index"]))
        self.call._send = send

        def worker(op, **kwargs):
            if op == "transcribe":
                return {"text": "That is Squadron 42."}
            if op == "synthesize_stream":
                kwargs["on_audio"]({"audio": audio(rate=24000, channels=1), "seconds": .04})
                return {}
            raise AssertionError(op)
        self.worker.request.side_effect = worker

    def test_reuses_history_and_accepted_reply_before_speaking(self):
        self.bridge.history.append({"role": "assistant", "content": "A letter sent earlier."})

        def turn(history, text, **kwargs):
            self.assertIs(history, self.bridge.history)
            self.assertEqual(kwargs["mode"], "discord_voice")
            self.assertEqual(text, "That is Squadron 42.")
            self.assertEqual(self.sent, [])
            history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": "I know that game."}])
            kwargs["on_event"]("tokens", {"prompt": 123})
            return "I know that game."

        with patch.object(chat, "one_turn", side_effect=turn), patch.object(chat, "friend_name", return_value="Friend"), \
                patch.object(config, "LIVE_VOICE_BACKEND", "kyutai"), \
                patch.object(config, "DISCORD_VOICE_TRANSCRIPTS", True):
            self.call._turn(0, audio())
        self.assertTrue(self.worker.request.call_args_list[0].kwargs["discord_input"])
        self.assertEqual([m["op"] for m in self.sent], ["begin", "audio", "end"])
        self.bridge._send_reply.assert_called_once_with("I know that game.")
        self.assertEqual(self.bridge.last_tokens, {"prompt": 123})
        job = self.sent[0]["job"]
        self.call._receipt({"job": job, "partial": False, "reason": "sent to Discord"})
        self.assertIn("hearing it on the client is not confirmed", self.bridge.history[-1]["_voice_delivery"])

    def test_interrupt_during_brain_work_preserves_written_turn_but_skips_speech(self):
        def turn(history, text, **kwargs):
            history.append({"role": "assistant", "content": "A generated reply."})
            self.call.interrupt()
            return "A generated reply."
        with patch.object(chat, "one_turn", side_effect=turn), patch.object(chat, "friend_name", return_value="Friend"):
            self.call._turn(0, audio())
        self.assertEqual(self.sent, [])
        self.assertIn("interrupted", self.bridge.history[-1]["_voice_delivery"].lower())
        self.bridge._send_reply.assert_not_called()

    def test_empty_transcription_never_calls_brain(self):
        self.worker.request.return_value = {"text": ""}
        self.worker.request.side_effect = None
        with patch.object(chat, "one_turn") as brain, patch.object(chat, "friend_name", return_value="Friend"):
            self.call._turn(0, audio())
        brain.assert_not_called()

    def test_rejected_capture_keeps_discord_playback_and_its_timer(self):
        self.worker.request.side_effect=None;self.worker.request.return_value={'text':''}
        self.call.initiative.start();self.call.initiative.heard()
        self.call.initiative.speaking('playing');self.call.jobs['playing']={'role':'assistant','content':'A story.'}
        epoch=self.call.epoch;version=self.call.initiative.version
        with patch.object(chat,'one_turn') as brain,patch.object(chat,'friend_name',return_value='Fixture'):
            self.call._turn(epoch,audio())
        brain.assert_not_called();self.assertEqual(self.sent,[])
        self.assertEqual(self.call.epoch,epoch);self.assertEqual(self.call.initiative.version,version)
        self.assertEqual(self.call.initiative.job,'playing')

    def test_accepted_words_stop_existing_discord_audio_before_the_brain_turn(self):
        self.call.jobs['playing']={'role':'assistant','content':'A story.'}
        def turn(history,text,**kwargs):
            self.assertEqual(self.sent,[{'op':'stop'}])
            history.extend([{'role':'user','content':text},{'role':'assistant','content':'Okay.'}])
            return 'Okay.'
        with patch.object(chat,'one_turn',side_effect=turn),patch.object(chat,'friend_name',return_value='Fixture'):
            self.call._turn(0,audio())
        self.assertEqual([item['op'] for item in self.sent],['stop','begin','audio','end'])

    def test_typed_dm_supersedes_private_choices_during_a_call(self):
        incoming=discord_bridge.DiscordBridge.__new__(discord_bridge.DiscordBridge)
        incoming.chat_id=123;incoming.call=self.call
        incoming.turn=Mock();incoming.fold_if_due=Mock()
        self.call.initiative.start();self.call.initiative.heard()
        self.call.initiative.speaking('first');self.call.initiative.finished('first');self.call.initiative.due=0
        claim=self.call.initiative.claim();commit=Mock()
        with patch.object(discord_bridge,'_say'):
            incoming.handle({'channel_id':'123','content':'Please wait.'})
        incoming.turn.assert_called_once_with('Please wait.')
        self.assertEqual(self.sent,[{'op':'stop'}])
        self.assertFalse(self.call.initiative.resolve(claim,{'text':'Stale words.'},commit))
        commit.assert_not_called()

    def test_initiative_continues_same_visit_and_waits_for_transport_completion(self):
        self.bridge.history=[{'role':'user','content':'Keep telling the story.'}]
        self.call.initiative.start();self.call.initiative.heard()
        self.call.initiative.speaking('first');self.call.initiative.finished('first')
        self.call.initiative.due=0
        decision={'text':'The story continues.','next_check_seconds':12,'wait_for_user':False}
        additions=[{'role':'user','content':'quiet event','_engine':True},{'role':'assistant','content':decision['text']}]
        with patch.object(discord_voice.voice_initiative,'decide',return_value=(decision,additions)), \
             patch.object(config,'LIVE_VOICE_BACKEND','kyutai'):
            self.call._initiative()
        self.assertEqual([m['op'] for m in self.sent],['begin','audio','end'])
        self.assertEqual(self.bridge.history[-1]['content'],'The story continues.')
        self.assertIsNone(self.call.initiative.due)
        job=self.sent[0]['job']
        self.call._receipt({'job':job,'partial':False,'reason':'sent to Discord'})
        self.assertIsNotNone(self.call.initiative.due)
        self.assertFalse(self.call.jobs)

    def test_interrupted_initiative_cannot_append_or_speak_a_stale_decision(self):
        self.call.initiative.start();self.call.initiative.heard()
        self.call.initiative.speaking('first');self.call.initiative.finished('first');self.call.initiative.due=0
        def decide(*args,**kwargs):
            self.call.interrupt()
            return {'text':'Stale words.'},[{'role':'assistant','content':'Stale words.'}]
        with patch.object(discord_voice.voice_initiative,'decide',side_effect=decide):
            self.call._initiative()
        self.assertEqual(self.bridge.history,[])
        self.assertEqual(self.sent,[])

    def test_old_helper_exit_does_not_hang_up_a_restarted_call(self):
        old = SimpleNamespace(stdout=iter([]))
        self.call.proc = Mock()
        self.call._read(old)
        self.assertTrue(self.call.active)
        self.assertFalse(self.call.ready.is_set())

    def test_discord_spoken_hint_is_transient_and_has_the_right_place(self):
        t = {"role": "user", "content": "Hello.", "_voice_mode": True, "_voice_discord": True}
        rendered = chat.render_turn(t)
        self.assertIn("in a Discord voice channel", rendered["content"])
        self.assertNotIn("in the parlor", rendered["content"])
        self.assertEqual(t["content"], "Hello.")


class CommandTests(unittest.TestCase):
    def make_bridge(self):
        b = object.__new__(discord_bridge.DiscordBridge)
        b.chat_id = 100000000000000001; b._call_owner = ""; b.call = None
        b.token = "fixture-token"; b.send = Mock(); b.last_activity = 0
        b.rest = Mock(return_value={"recipients": [{"id": "100000000000000002"}]})
        return b

    def test_only_paired_channel_can_start_call(self):
        b = self.make_bridge()
        with patch.object(discord_voice, "Call") as call:
            b.handle({"channel_id": "100000000000000999", "content": "!call 100000000000000003"})
        call.assert_not_called()

    def test_call_resolves_owner_from_dm_and_passes_only_the_requested_channel(self):
        b = self.make_bridge()
        with patch.object(discord_voice, "Call") as klass:
            self.assertTrue(b.command("/call 100000000000000003"))
            klass.return_value.start.assert_called_once_with("100000000000000003", "100000000000000002")
        b.rest.assert_called_once_with("GET", "/channels/100000000000000001")

    def test_group_dm_is_refused(self):
        b = self.make_bridge()
        b.rest.return_value = {"recipients": [{"id": "1"}, {"id": "2"}]}
        with patch.object(discord_voice, "Call") as klass:
            b.command("/call 100000000000000003")
            klass.return_value.start.assert_not_called()

    def test_hangup_does_not_clear_conversation(self):
        b = self.make_bridge(); b.call = Mock(); b.history = [{"content": "saved"}]
        b.command("/hangup")
        b.call.close.assert_called_once()
        self.assertEqual(b.history, [{"content": "saved"}])

    def test_replacing_the_visit_is_blocked_while_call_turn_finishes(self):
        b = self.make_bridge(); b.call = SimpleNamespace(active=True)
        with patch.object(discord_bridge.telegram.Bridge, "command") as parent:
            b.command("/new")
        parent.assert_not_called()


class OwnershipTests(unittest.TestCase):
    def test_process_lease_is_reentrant_but_excludes_other_processes(self):
        with tempfile.TemporaryDirectory() as d, patch.object(config, "ROOT", Path(d)):
            a = voice_lock.Lease("fixture").acquire(); b = voice_lock.Lease("fixture").acquire()
            script = (
                "import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
                "import config,voice_lock;config.ROOT=Path(sys.argv[2]);"
                "voice_lock.Lease('fixture').acquire()"
            )
            args = [sys.executable, "-c", script, str(Path(voice_lock.__file__).parent), d]
            try:
                blocked = subprocess.run(args, capture_output=True, text=True, timeout=15)
                self.assertNotEqual(blocked.returncode, 0)
                a.release()
                self.assertNotEqual(subprocess.run(args, capture_output=True, timeout=15).returncode, 0)
            finally:
                a.release(); b.release()
            self.assertEqual(subprocess.run(args, capture_output=True, timeout=15).returncode, 0)

    def test_call_prevents_unload_by_a_different_process(self):
        with tempfile.TemporaryDirectory() as d, patch.object(config, "ROOT", Path(d)):
            script = (
                "import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
                "import config,voice_lock;config.ROOT=Path(sys.argv[2]);"
                "l=voice_lock.Lease('brain').acquire();print('ready',flush=True);sys.stdin.readline()"
            )
            p = subprocess.Popen([sys.executable, "-c", script, str(Path(voice_lock.__file__).parent), d],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(p.stdout.readline().strip(), "ready")
                with patch.object(ollama_client, "_unload") as unload, patch.object(ollama_client, "_chat") as brain:
                    with self.assertRaises(voice_lock.Busy):
                        ollama_client.unload("fixture")
                    unload.assert_not_called()
                    with self.assertRaises(ollama_client.BrainUnavailable):
                        ollama_client.chat([])
                    brain.assert_not_called()
            finally:
                p.stdin.write("done\n"); p.stdin.flush(); p.wait(timeout=15)
                p.stdin.close(); p.stdout.close()

    def test_continuous_call_remains_a_live_visit(self):
        with patch.object(doors, "status", return_value={"pid": 1}), patch.object(chat, "VISIT_FILE") as marker:
            self.assertTrue(chat.visit_live())
            marker.stat.assert_not_called()

    def test_parlor_process_blocks_call_before_transport_or_models_start(self):
        c = discord_voice.Call(bridge(), Mock())
        with patch.object(config, "DISCORD_VOICE_ENABLED", True), patch.object(doors, "status", return_value={"pid": 1}), \
                patch.object(discord_voice.subprocess, "Popen") as proc:
            with self.assertRaisesRegex(ValueError, "Close Parlor"):
                c.start("100000000000000003", "100000000000000002")
            proc.assert_not_called()

    def test_worker_start_failure_releases_model_lease(self):
        with tempfile.TemporaryDirectory() as d, patch.object(config, "ROOT", Path(d)), \
                patch.object(config, "LIVE_VOICE_PYTHON", sys.executable), \
                patch.object(live_voice.subprocess, "Popen", side_effect=OSError("fixture error")):
            worker = live_voice.Worker()
            with self.assertRaises(live_voice.VoiceError):
                worker._start("kyutai")
            self.assertIsNone(worker.model_lease)
            self.assertNotIn((Path(d) / ".runtime" / "live-voice-models.lock").resolve(), voice_lock._held)

    def test_failed_audio_consumer_resets_the_worker_protocol(self):
        worker = live_voice.Worker()
        worker._start = Mock(); worker.close = Mock()
        worker.proc = SimpleNamespace(stdin=Mock())
        worker.responses.put({"id": "fixture-job", "event": "audio", "audio": "fixture"})
        with patch.object(live_voice.uuid, "uuid4", return_value=SimpleNamespace(hex="fixture-job")):
            with self.assertRaisesRegex(ValueError, "consumer failed"):
                worker.request("synthesize_stream", on_audio=Mock(side_effect=ValueError("consumer failed")))
        worker.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
