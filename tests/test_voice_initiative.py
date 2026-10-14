"""Silence, contextual choices and preemption, with no live model or personal writes."""
import base64
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import subprocess
import threading
import time
import unittest
import wave
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))
import chat
import config
import live_voice
import ollama_client
import parlor
import voice_initiative as initiative


class Time:
    value = 0.0
    def __call__(self):
        return self.value
    def advance(self, seconds):
        self.value += seconds


def choice(action="speak", text="Let the story carry on.", seconds=10, wait=False):
    return {"role": "assistant", "content": "", "thinking": "Private deliberation.",
            "tool_calls": [{"function": {"name": "voice_initiative", "arguments": {
                "action": action, "text": text, "next_check_seconds": seconds, "wait_for_user": wait}}}]}


def history():
    return [{"role": "user", "content": "Talk me to sleep; it may take thirty minutes.",
             "_system": "You are a fixture friend."},
            {"role": "assistant", "content": "Let me begin a story."}]


class ClockTests(unittest.TestCase):
    def setUp(self):
        logging = patch.object(config, "LIVE_VOICE_INITIATIVE_LOG", False)
        logging.start(); self.addCleanup(logging.stop)
        self.time = Time()
        self.sample = Mock(return_value=25)
        self.clock = initiative.Clock(self.time, self.sample)
        self.clock.start()
        self.clock.heard()

    def arm(self):
        self.clock.speaking("first")
        self.clock.finished("first")

    def test_only_completed_playback_starts_the_clock_and_sample_is_kept(self):
        self.time.advance(120)
        self.assertIsNone(self.clock.claim())
        self.clock.speaking("first")
        self.time.advance(120)
        self.assertIsNone(self.clock.claim())
        self.clock.finished("other")
        self.assertIsNone(self.clock.claim())
        self.clock.finished("first")
        self.clock.finished("first")  # duplicate receipt cannot move the deadline
        self.time.advance(24)
        self.assertIsNone(self.clock.claim())
        self.time.advance(1)
        claim = self.clock.claim()
        self.assertEqual(claim[2], {"since_user_s": 265, "since_spoken_s": 25, "followups": 0})
        self.sample.assert_called_once()
        self.assertIsNone(self.clock.claim())  # one model call at a time

    def test_normal_outliers_are_bounded_and_zero_spread_is_supported(self):
        for sample, delay in [(-100, 5), (1000, 90)]:
            self.sample.return_value = sample
            self.clock.start(); self.clock.heard(); self.arm()
            self.assertEqual(self.clock.due - self.time(), delay)
        with patch.object(config, "LIVE_VOICE_INITIATIVE_STD_S", 0):
            self.clock.start(); self.clock.heard(); self.arm()
            self.assertEqual(self.sample.call_args.args[1], 0)

    def test_interruption_never_counts_as_completed_speech(self):
        self.clock.speaking("first")
        self.clock.finished("first", interrupted=True)
        self.time.advance(100)
        self.assertIsNone(self.clock.claim())
        self.assertIsNone(self.clock.last_spoken)

    def test_waiting_reschedules_without_speaking_and_until_user_stays_quiet(self):
        self.arm(); self.time.advance(25)
        claim = self.clock.claim()
        self.clock.resolve(claim, {"text": "", "next_check_seconds": 100}, lambda: None)
        self.time.advance(99); self.assertIsNone(self.clock.claim())
        self.time.advance(1)
        claim = self.clock.claim()
        self.clock.resolve(claim, {"text": "", "wait_for_user": True}, lambda: None)
        self.time.advance(3600); self.assertIsNone(self.clock.claim())
        self.clock.empty_capture(); self.assertIsNone(self.clock.due)
        self.clock.heard(); self.arm(); self.time.advance(25)
        self.assertIsNotNone(self.clock.claim())

    def test_input_stop_and_restart_supersede_an_inflight_choice(self):
        for action in (self.clock.interrupt, self.clock.heard, self.clock.stop, self.clock.start):
            self.clock.start(); self.clock.heard(); self.arm(); self.time.advance(25)
            claim = self.clock.claim(); action()
            commit = Mock()
            self.assertFalse(self.clock.resolve(claim, {"text": "Stale words."}, commit))
            commit.assert_not_called()
            self.assertIsNone(self.clock.inflight)

    def test_rejected_noise_preserves_the_existing_timer_and_never_rearms_a_manual_mute(self):
        self.arm(); self.time.advance(10); version=self.clock.version; self.clock.empty_capture()
        self.assertEqual(self.clock.due, 25)
        self.assertEqual(self.clock.last_user, 0)
        self.assertEqual(self.clock.version, version)
        self.clock.interrupt(); self.clock.empty_capture()
        self.assertIsNone(self.clock.due)

    def test_half_hour_can_span_many_passages_without_new_user_turns(self):
        self.arm()
        for index in range(61):
            self.time.advance(25)
            claim = self.clock.claim()
            self.assertIsNotNone(claim)
            self.assertEqual(claim[2]["followups"], index)
            job = f"passage-{index}"
            self.clock.resolve(claim, {"text": "Another passage.", "next_check_seconds": 25},
                               lambda: self.clock.speaking(job))
            self.time.advance(5)
            self.assertIsNone(self.clock.claim())
            self.clock.finished(job)
        self.assertEqual(self.clock.last_user, 0)
        self.assertGreater(self.time(), 1800)

    def test_disabled_initiative_never_calls_or_commits_a_choice(self):
        self.arm(); self.time.advance(25)
        with patch.object(config, "LIVE_VOICE_INITIATIVE", False):
            self.assertIsNone(self.clock.claim())
        claim = self.clock.claim(); commit = Mock()
        with patch.object(config, "LIVE_VOICE_INITIATIVE", False):
            self.assertFalse(self.clock.resolve(claim, {"text": "Words."}, commit))
        commit.assert_not_called()


class DecisionTests(unittest.TestCase):
    def setUp(self):
        logging = patch.object(config, "LIVE_VOICE_INITIATIVE_LOG", False)
        logging.start(); self.addCleanup(logging.stop)

    def decide(self, message, **kwargs):
        with patch.object(ollama_client, "chat", return_value=message) as brain:
            result, additions = initiative.decide(history(),
                {"since_user_s": 1750, "since_spoken_s": 10, "followups": 45}, **kwargs)
        return result, additions, brain

    def test_context_and_elapsed_time_are_internal_and_spoken_words_are_visible(self):
        original = history()
        with patch.object(config, "LIVE_VOICE_INITIATIVE_THINK", True), \
             patch.object(ollama_client, "chat", return_value=choice()) as brain:
            result, additions = initiative.decide(original,
                {"since_user_s": 1750, "since_spoken_s": 10, "followups": 45})
        self.assertEqual(len(original), 2)
        self.assertEqual(result["text"], "Let the story carry on.")
        sent = brain.call_args.args[0]
        self.assertIn("thirty minutes", sent[1]["content"])
        self.assertIn("1750 seconds", sent[-1]["content"])
        self.assertIn("45 additional turns", sent[-1]["content"])
        self.assertIn("Silence alone", sent[-1]["content"])
        self.assertEqual(brain.call_args.kwargs["tools"], initiative.voice_tools())
        self.assertTrue(brain.call_args.kwargs["think"])
        self.assertFalse(brain.call_args.kwargs["expect_words"])
        self.assertTrue(all(t.get("_engine") for t in additions[:-1]))
        self.assertFalse(additions[-1].get("_engine", False))
        with tempfile.TemporaryDirectory() as temp, patch.object(chat, "friend_name", return_value="Fixture"):
            path = Path(temp) / "visit.md"
            chat.save_transcript(original + additions, path=path)
            transcript = path.read_text(encoding="utf-8")
        self.assertIn("Let the story carry on.", transcript)
        self.assertNotIn("quiet moment", transcript)
        self.assertNotIn("voice_initiative", transcript)
        self.assertNotIn("Private deliberation", transcript)
        self.assertEqual(transcript.count(f"**{config.USER_NAME}:**"), 1)

    def test_wait_reason_and_content_are_never_spoken(self):
        msg = choice(action="wait", text="This is a private reason.", wait=True)
        msg["content"] = "I should let him rest."
        result, additions, _ = self.decide(msg)
        self.assertEqual(result["text"], "")
        self.assertTrue(result["wait_for_user"])
        self.assertTrue(all(t.get("_engine") for t in additions))

    def test_json_string_arguments_work_and_timing_is_bounded(self):
        msg = choice(seconds=1e20)
        msg["tool_calls"][0]["function"]["arguments"] = json.dumps(msg["tool_calls"][0]["function"]["arguments"])
        result, _, _ = self.decide(msg)
        self.assertEqual(result["next_check_seconds"], 600)
        for value in (float("nan"), float("inf"), "bad", None):
            result, _, _ = self.decide(choice(seconds=value))
            self.assertEqual(result["next_check_seconds"], 60)

    def test_invalid_or_external_tool_calls_fail_quietly(self):
        unknown = choice(); unknown["tool_calls"][0]["function"]["name"] = "run_python"
        invalid = choice(); invalid["tool_calls"][0]["function"]["arguments"] = "broken"
        missing = choice(); missing["tool_calls"][0]["function"]["arguments"].pop("text")
        for msg in (unknown, invalid, missing, {"content": "Private thoughts without a choice."}):
            with patch.object(chat.tools, "dispatch") as dispatch:
                result, additions, _ = self.decide(msg)
            dispatch.assert_not_called()
            self.assertEqual(result["text"], "")
            self.assertEqual(additions, [])

    def test_phone_placement_and_thinking_setting_reach_the_model(self):
        with patch.object(config, "LIVE_VOICE_INITIATIVE_THINK", False):
            _, _, brain = self.decide(choice(), mode="phone_voice", physicalize=True,
                                     physicalize_voice={"enabled": True, "close": True, "ear": "left", "distance_cm": 2})
        prompt = brain.call_args.args[0][-1]["content"]
        self.assertIn("phone app", prompt)
        self.assertIn("keeper's left ear", prompt)
        self.assertFalse(brain.call_args.kwargs["think"])

    def test_private_choice_works_without_a_thinking_channel_at_the_wire(self):
        message=choice();message.pop('thinking')
        for enabled in (False,True):
            with self.subTest(private_thinking=enabled), \
                 patch.object(config,'CHAT_THINK',False), \
                 patch.object(config,'LIVE_VOICE_INITIATIVE_THINK',enabled), \
                 patch.object(ollama_client,'_post',return_value={'message':message}) as post:
                decision,additions=initiative.decide(history(),{'since_user_s':45,'since_spoken_s':25,'followups':0})
            self.assertIs(post.call_args.args[1]['think'],enabled)
            self.assertEqual(decision['text'],'Let the story carry on.')
            self.assertEqual(additions[-1]['content'],'Let the story carry on.')
            self.assertTrue(all(t.get('_engine') for t in additions[:-1]))

    def test_human_voice_turn_and_private_check_share_the_tool_prefix(self):
        for mode in ('voice','phone_voice','discord_voice'):
            turns=[]
            with patch.object(chat.assemble,'system_prompt',return_value='Fixture identity'), \
                 patch.object(chat.assemble,'moment',return_value=('',[])), \
                 patch.object(chat,'mark_visit_live'),patch.object(chat.tools,'refresh_her_tools'), \
                 patch.object(ollama_client,'chat',return_value={'role':'assistant','content':'Hello.'}) as brain:
                chat.one_turn(turns,'Hello',mode=mode,on_event=lambda *_:None)
                regular_tools=brain.call_args.kwargs['tools']
            _,_,private=self.decide(choice(),mode=mode)
            self.assertEqual(regular_tools,private.call_args.kwargs['tools'])

    def test_private_control_tool_on_a_real_user_turn_does_not_execute_or_schedule(self):
        with patch.object(chat.assemble,'system_prompt',return_value='Fixture identity'), \
             patch.object(chat.assemble,'moment',return_value=('',[])),patch.object(chat,'mark_visit_live'), \
             patch.object(chat.tools,'refresh_her_tools'),patch.object(chat.tools,'dispatch') as dispatch, \
             patch.object(ollama_client,'chat',side_effect=[choice(),{'role':'assistant','content':'Hello.'}]):
            self.assertEqual(chat.one_turn([],'Hello',mode='voice',on_event=lambda *_:None),'Hello.')
        dispatch.assert_not_called()


class Speech:
    def request(self, op, **kwargs):
        assert op == "synthesize"
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setframerate(16000); wav.setnchannels(1); wav.setsampwidth(2)
            wav.writeframes(b"\0\0" * 160)
        return {"audio": base64.b64encode(buffer.getvalue()).decode(), "seconds": .01}


class SessionTests(unittest.TestCase):
    def setUp(self):
        logging = patch.object(config, "LIVE_VOICE_INITIATIVE_LOG", False)
        logging.start(); self.addCleanup(logging.stop)
        self.session = parlor.Session()
        self.session.history = history()
        self.session._checkpoint = Mock()
        self.session.playback = live_voice.Playback(Speech(), stream=False)
        self.time = Time()
        self.session.initiative = initiative.Clock(self.time, lambda *_: 25)
        self.session.initiative.start(); self.session.initiative.heard()
        self.session.initiative.speaking("first"); self.session.initiative.finished("first")
        self.time.advance(25)

    def test_continuation_plays_normally_and_receipt_starts_the_next_pause(self):
        with patch.object(ollama_client, "chat", return_value=choice(seconds=8)):
            result = self.session.voice_tick()
        self.assertEqual(result["reply"], "Let the story carry on.")
        self.assertNotIn("transcript", result)
        job = result["speech_job"]
        deadline = time.monotonic() + 2
        while not self.session.playback.status(job)["done"] and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertIsNone(self.session.initiative.due)
        self.time.advance(20)
        self.session.delivery(job, 1, False)
        self.assertEqual(self.session.initiative.due, self.time() + 8)
        self.assertIn("completed", self.session.history[-1]["_voice_delivery"])

    def test_new_speech_interrupts_a_pending_choice_and_no_stale_turn_is_saved(self):
        entered, release = threading.Event(), threading.Event()
        def brain(*args, **kwargs):
            entered.set(); release.wait(2); return choice()
        results = []
        with patch.object(ollama_client, "chat", side_effect=brain):
            thread = threading.Thread(target=lambda: results.append(self.session.voice_tick()))
            thread.start(); self.assertTrue(entered.wait(2))
            self.session.interrupt_voice(); release.set(); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [{"quiet": True}])
        self.assertEqual(self.session.history, history())
        self.assertIsNone(self.session.playback.current)
        self.session._checkpoint.assert_not_called()

    def test_stop_discards_an_inflight_choice_and_offline_backoff_does_not_break_chat(self):
        def brain(*args, **kwargs):
            self.session.initiative.stop(); return choice()
        with patch.object(ollama_client, "chat", side_effect=brain):
            self.assertEqual(self.session.voice_tick(), {"quiet": True})
        self.assertEqual(self.session.history, history())
        self.session.initiative.start(); self.session.initiative.heard()
        self.session.initiative.speaking("next"); self.session.initiative.finished("next"); self.time.advance(25)
        with patch.object(ollama_client, "chat", side_effect=ollama_client.BrainUnavailable("offline")):
            result = self.session.voice_tick()
        self.assertIn("offline", result["error"])
        self.assertEqual(self.session.initiative.due, self.time() + 60)
        self.assertEqual(self.session.history, history())

    def test_discarding_an_old_job_cannot_cancel_a_new_reply(self):
        playback = self.session.playback
        old = playback.start("Old words.", playback.epoch, {})
        new = playback.start("New words.", playback.cancel(), {})
        epoch = playback.epoch
        self.session.interrupt_voice(old)
        self.assertFalse(playback.status(new)["cancelled"])
        self.assertEqual(playback.epoch, epoch)
        playback.cancel()

    def test_rejected_audio_preserves_playback_history_and_pending_initiative(self):
        clock=self.session.initiative
        job=self.session.playback.start('Still speaking.',self.session.playback.epoch,{})
        clock.speaking(job);version=clock.version;epoch=self.session.playback.epoch
        with patch.object(chat,'friend_name',return_value='Fixture'):
            recognized=self.session.transcribe_voice(Speech().request('synthesize')['audio'],
                worker=Mock(request=Mock(return_value={'text':''})),mode='phone_voice')
        self.assertEqual(self.session.accept_voice(recognized),{'empty':True})
        self.assertFalse(self.session.playback.status(job)['cancelled'])
        self.assertEqual(self.session.playback.epoch,epoch)
        self.assertEqual(clock.version,version);self.assertEqual(clock.job,job)
        self.assertEqual(self.session.history,history())
        self.session.playback.cancel()
        clock.job=None;clock.due=self.time();claim=clock.claim()
        clock.empty_capture()
        self.assertTrue(clock.resolve(claim,{'text':'','next_check_seconds':60},lambda:None))

    def test_recognition_blocks_new_initiative_claims_and_late_commits(self):
        entered, release = threading.Event(), threading.Event()
        def recognize(*args, **kwargs):
            entered.set(); release.wait(2); return {'text': ''}
        clock = self.session.initiative
        claim = clock.claim()
        self.assertIsNotNone(claim)
        recognized = []
        with patch.object(chat, 'friend_name', return_value='Fixture'):
            thread = threading.Thread(target=lambda: recognized.append(self.session.transcribe_voice(
                Speech().request('synthesize')['audio'], worker=Mock(request=recognize))))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                version, epoch = clock.version, self.session.playback.epoch
                commit = Mock()
                self.assertFalse(clock.resolve(claim, {'text': 'Too late.'}, commit))
                commit.assert_not_called()
                self.time.advance(60)
                self.assertIsNone(clock.claim())
                self.assertEqual(clock.version, version)
                self.assertEqual(self.session.playback.epoch, epoch)
            finally:
                release.set(); thread.join(2)
        self.assertEqual(clock.recognizing, 0)
        self.assertEqual(self.session.accept_voice(recognized[0]), {'empty': True})
        self.assertIsNotNone(clock.claim())

    def test_prompt_accepted_after_initiative_start_still_cancels_that_job(self):
        with patch.object(chat, 'friend_name', return_value='Fixture'):
            recognized = self.session.transcribe_voice(Speech().request('synthesize')['audio'],
                worker=Mock(request=Mock(return_value={'text': 'My actual prompt.'})))
        with patch.object(ollama_client, 'chat', return_value=choice()):
            result = self.session.voice_tick()
        self.assertIn('speech_job', result)
        accepted = self.session.accept_voice(recognized)
        self.assertTrue(accepted['accepted'])
        self.assertTrue(self.session.playback.status(result['speech_job'])['cancelled'])
        self.assertEqual(self.session.take_voice_input(accepted['input'])['text'], 'My actual prompt.')

    def test_failed_recognition_releases_initiative_hold(self):
        with patch.object(chat, 'friend_name', return_value='Fixture'), self.assertRaises(RuntimeError):
            self.session.transcribe_voice(Speech().request('synthesize')['audio'],
                worker=Mock(request=Mock(side_effect=RuntimeError('failed'))))
        self.assertEqual(self.session.initiative.recognizing, 0)
        self.assertIsNotNone(self.session.initiative.claim())

    def test_only_accepted_words_cancel_and_create_a_single_use_prompt(self):
        job=self.session.playback.start('Still speaking.',self.session.playback.epoch,{})
        self.session.initiative.speaking(job)
        with patch.object(chat,'friend_name',return_value='Fixture'):
            recognized=self.session.transcribe_voice(Speech().request('synthesize')['audio'],
                worker=Mock(request=Mock(return_value={'text':'Please stop.'})))
        self.assertFalse(self.session.playback.status(job)['cancelled'])
        accepted=self.session.accept_voice(recognized)
        self.assertTrue(accepted['accepted'])
        self.assertTrue(self.session.playback.status(job)['cancelled'])
        # A late discarded autonomous result targets the already cancelled
        # old job; it must not revoke this newer accepted prompt.
        self.session.interrupt_voice(job)
        prompt=self.session.take_voice_input(accepted['input'])
        self.assertEqual(prompt['text'],'Please stop.')
        self.assertEqual(self.session.history,history())
        with self.assertRaises(ValueError):self.session.take_voice_input(accepted['input'])

    def test_manual_stop_during_recognition_or_after_acceptance_cannot_resurrect_a_prompt(self):
        recognized={'text':'Late words.','generation':self.session.generation,'epoch':self.session.playback.epoch,
                    'recognition_s':.1,'mode':'phone_voice'}
        self.session.interrupt_voice()
        self.assertEqual(self.session.accept_voice(recognized),{'cancelled':True})
        recognized['epoch']=self.session.playback.epoch
        accepted=self.session.accept_voice(recognized)
        self.session.interrupt_voice()
        with self.assertRaises(ValueError):self.session.take_voice_input(accepted['input'])


class ConsoleTests(unittest.TestCase):
    def test_process_shutdown_drains_queued_console_output(self):
        script = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "import voice_initiative as v; "
            "[v.console_log('voice', 'shutdown fixture '+str(i)) for i in range(100)]"
        )
        result = subprocess.run([sys.executable, '-c', script, str(Path(initiative.__file__).parent)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('shutdown fixture 99', result.stdout)

    def setUp(self):
        for name, value in [("LIVE_VOICE_INITIATIVE_LOG", True),
                            ("LIVE_VOICE_INITIATIVE_LOG_THINKING", False),
                            ("LIVE_VOICE_INITIATIVE_THINK", False)]:
            setting = patch.object(config, name, value)
            setting.start(); self.addCleanup(setting.stop)

    def test_reason_and_timing_are_logged_with_thinking_off_and_never_spoken(self):
        message = choice(action="wait", text="", seconds=45)
        message["tool_calls"][0]["function"]["arguments"]["reason"] = "You asked for some quiet."
        output = io.StringIO()
        with redirect_stdout(output), patch.object(ollama_client, "chat", return_value=message) as brain:
            result, additions = initiative.decide(history(),
                {"since_user_s": 40, "since_spoken_s": 15, "followups": 1}, mode="phone_voice")
        self.assertTrue(initiative.flush_console())
        self.assertEqual(brain.call_count, 1)
        self.assertFalse(brain.call_args.kwargs["think"])
        self.assertIn("private reason field", brain.call_args.args[0][-1]["content"])
        self.assertIn("[voice/phone] Model proposed WAIT", output.getvalue())
        self.assertIn("next pause 45s", output.getvalue())
        self.assertIn("You asked for some quiet.", output.getvalue())
        self.assertNotIn("Private deliberation", output.getvalue())
        self.assertEqual(result["text"], "")
        self.assertTrue(all(t.get("_engine") for t in additions))
        with tempfile.TemporaryDirectory() as folder, patch.object(chat, "friend_name", return_value="Fixture"):
            path = Path(folder) / "visit.md"
            chat.save_transcript(history() + additions, path=path)
            self.assertNotIn("You asked for some quiet.", path.read_text(encoding="utf-8"))

    def test_optional_reasoning_log_does_not_enable_thinking_or_change_the_choice(self):
        output = io.StringIO()
        with patch.object(config, "LIVE_VOICE_INITIATIVE_LOG_THINKING", True), redirect_stdout(output), \
             patch.object(ollama_client, "chat", return_value=choice()) as brain:
            result, _ = initiative.decide(history(), {"since_user_s": 45, "since_spoken_s": 20, "followups": 0})
        self.assertTrue(initiative.flush_console())
        self.assertFalse(brain.call_args.kwargs["think"])
        self.assertEqual(result["text"], "Let the story carry on.")
        self.assertIn("Private deliberation.", output.getvalue())
        self.assertIn("Proposed speech", output.getvalue())

    def test_clock_logs_actual_sample_indefinite_wait_and_discarded_decisions_without_poll_spam(self):
        now = Time(); clock = initiative.Clock(now, lambda *_: 12.5, source="phone")
        output = io.StringIO()
        with redirect_stdout(output):
            clock.start(); clock.heard(); clock.speaking("reply"); clock.finished("reply")
            self.assertTrue(initiative.flush_console())
            before_polls = output.getvalue()
            for _ in range(10):
                self.assertIsNone(clock.claim())
            self.assertTrue(initiative.flush_console())
            self.assertEqual(output.getvalue(), before_polls)
            now.advance(12.5); claim = clock.claim()
            clock.resolve(claim, {"text": "", "wait_for_user": True}, lambda: None)
            clock.heard(); clock.speaking("another"); clock.finished("another")
            now.advance(12.5); claim = clock.claim(); clock.stop()
            self.assertFalse(clock.resolve(claim, {"text": "Stale speech."}, Mock()))
        self.assertTrue(initiative.flush_console())
        log = output.getvalue()
        self.assertIn("next check in 12.5s", log)
        self.assertIn("Check 1 started", log)
        self.assertIn("follow-ups 0", log)
        self.assertIn("no scheduled check", log)
        self.assertIn("Decision discarded", log)
        self.assertIn("Voice stopped", log)

    def test_disabled_logging_is_silent_and_invalid_choices_report_the_fallback(self):
        output = io.StringIO()
        with patch.object(config, "LIVE_VOICE_INITIATIVE_LOG", False), redirect_stdout(output), \
             patch.object(ollama_client, "chat", return_value=choice()) as brain:
            initiative.console_log("phone", "Hidden", thinking="Hidden reasoning")
            initiative.decide(history(), {"since_user_s": 40, "since_spoken_s": 15, "followups": 0})
        self.assertTrue(initiative.flush_console())
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("private reason field", brain.call_args.args[0][-1]["content"])
        output = io.StringIO()
        with redirect_stdout(output), patch.object(ollama_client, "chat", return_value={"content": "Wrong format"}):
            result, additions = initiative.decide(history(), {"since_user_s": 40, "since_spoken_s": 15, "followups": 0})
        self.assertTrue(initiative.flush_console())
        self.assertIn("Invalid model choice", output.getvalue())
        self.assertIn("retry in 60s", output.getvalue())
        self.assertEqual(result["text"], ""); self.assertEqual(additions, [])

    def test_console_encoding_and_broken_output_do_not_fail_the_decision(self):
        buffer = io.BytesIO()
        console = io.TextIOWrapper(buffer, encoding="cp1252", write_through=True)
        with redirect_stdout(console):
            initiative.console_log("phone", "Waiting", reason="A quiet moment 🌙")
        self.assertTrue(initiative.flush_console())
        self.assertIn(b"\\U0001f319", buffer.getvalue())
        broken = Mock(); broken.write.side_effect = OSError("closed console")
        with redirect_stdout(broken), patch.object(ollama_client, "chat", return_value=choice()):
            result, _ = initiative.decide(history(), {"since_user_s": 40, "since_spoken_s": 15, "followups": 0})
        self.assertEqual(result["text"], "Let the story carry on.")
        self.assertTrue(initiative.flush_console())

    def test_deadline_is_logged_without_any_client_request_and_repeated_blocks_are_limited(self):
        now=Time();clock=initiative.Clock(now,lambda *_:9,source='phone');output=io.StringIO()
        with redirect_stdout(output),patch.object(config,'LIVE_VOICE_INITIATIVE_LOG_INTERVAL_S',10):
            clock.start();clock.heard();clock.speaking('reply');clock.finished('reply')
            now.advance(8.99);clock.monitor();self.assertTrue(initiative.flush_console())
            self.assertNotIn('deadline reached',output.getvalue())
            version=clock.version;deadline=clock.due
            now.advance(.01);clock.monitor();self.assertTrue(initiative.flush_console())
            self.assertIn('Check deadline reached',output.getvalue())
            self.assertIn('no client status has been received',output.getvalue())
            before=output.getvalue();clock.monitor();now.advance(1);clock.monitor()
            self.assertTrue(initiative.flush_console());self.assertEqual(output.getvalue(),before)
            now.advance(9);clock.monitor();self.assertTrue(initiative.flush_console())
        self.assertEqual(output.getvalue().count('Check deadline reached'),1)
        self.assertEqual(output.getvalue().count('Check deferred'),2)
        self.assertEqual(clock.version,version);self.assertEqual(clock.due,deadline)
        self.assertEqual(clock.checks,0);self.assertIsNone(clock.inflight)

    def test_client_blockers_are_read_only_and_stale_reports_are_labelled(self):
        now=Time();clock=initiative.Clock(now,lambda *_:9,source='phone');output=io.StringIO()
        with redirect_stdout(output):
            clock.start();clock.heard();clock.speaking('reply');clock.finished('reply')
            now.advance(9);version=clock.version;deadline=clock.due
            clock.report_client({'blocked':['capture','microphone_stale','unknown'],'phase':'listening',
                                 'mic_age_ms':4000,'candidate_ms':123,'private_audio':'must not be logged'})
            clock.monitor();self.assertTrue(initiative.flush_console())
            self.assertIn('unfinished microphone capture',output.getvalue())
            self.assertIn('over 3s',output.getvalue());self.assertNotIn('must not be logged',output.getvalue())
            now.advance(4);clock.monitor();self.assertTrue(initiative.flush_console())
            self.assertIn('current client readiness is unknown',output.getvalue())
            self.assertIn('last reported:',output.getvalue())
            clock.report_client({'blocked':[],'phase':'listening','mic_age_ms':0});clock.monitor()
            claim=clock.claim();self.assertTrue(initiative.flush_console())
        self.assertEqual(version,clock.version)
        self.assertEqual(deadline,9)
        self.assertIn('waiting for an eligible client check request',output.getvalue())
        self.assertIn('started 4.0s after its deadline',output.getvalue())
        self.assertIsNotNone(claim)

    def test_running_model_and_superseded_request_have_progress_instead_of_silent_gaps(self):
        now=Time();clock=initiative.Clock(now,lambda *_:9,source='phone');output=io.StringIO()
        with redirect_stdout(output):
            clock.start();clock.heard();clock.speaking('reply');clock.finished('reply')
            now.advance(9);claim=clock.claim();now.advance(10);clock.monitor()
            clock.heard();clock.monitor()
            self.assertFalse(clock.resolve(claim,{'text':'Stale speech.'},Mock()))
        self.assertTrue(initiative.flush_console())
        self.assertIn('Check 1 still running after 10.0s',output.getvalue())
        self.assertIn('superseded and its result will be discarded',output.getvalue())
        self.assertIn('Decision discarded',output.getvalue())

    def test_server_busy_and_invalid_status_are_logged_without_changing_scheduling(self):
        now=Time();clock=initiative.Clock(now,lambda *_:9,source='phone');output=io.StringIO()
        with redirect_stdout(output):
            clock.start();clock.heard();clock.speaking('reply');clock.finished('reply');now.advance(9)
            clock.report_client(None);clock.report_client({'blocked':'bad','phase':'bad','mic_age_ms':float('nan')})
            clock.monitor(server_busy=True)
        self.assertTrue(initiative.flush_console())
        self.assertIn('server conversation lock is busy',output.getvalue())
        self.assertEqual(clock.due,9);self.assertEqual(clock.client_details,{})

    def test_console_that_blocks_cannot_block_the_clock(self):
        entered,release=threading.Event(),threading.Event()
        sink=Mock()
        def slow_write(text):
            entered.set();release.wait(2)
        sink.write.side_effect=slow_write
        now=Time();clock=initiative.Clock(now,lambda *_:9,source='phone')
        try:
            with redirect_stdout(sink):
                initiative.console_log('phone','Slow console')
                self.assertTrue(entered.wait(1))
                done=threading.Event()
                def drive():
                    clock.start();clock.heard();clock.speaking('reply');clock.finished('reply')
                    now.advance(9);clock.monitor();clock.claim();done.set()
                thread=threading.Thread(target=drive);thread.start()
                self.assertTrue(done.wait(.5),'clock waited on the console sink')
                thread.join(1)
        finally:
            release.set();self.assertTrue(initiative.flush_console())

    def test_missing_reasoning_and_model_timing_details_are_explicit(self):
        message=choice(seconds=1000);message.pop('thinking')
        message['tokens']={'load_s':1.5,'prompt_s':100,'reply_s':2.5,'prompt':12345,'reply':90}
        output=io.StringIO()
        with redirect_stdout(output),patch.object(config,'LIVE_VOICE_INITIATIVE_LOG_THINKING',True), \
             patch.object(ollama_client,'chat',return_value=message):
            result,_=initiative.decide(history(),{'since_user_s':40,'since_spoken_s':15,'followups':0})
        self.assertTrue(initiative.flush_console())
        self.assertIn('prompt reading 100.00s',output.getvalue())
        self.assertIn('none (thinking is disabled)',output.getvalue())
        self.assertIn('model did not provide a private reason',output.getvalue())
        self.assertIn('requested interval 1000, effective interval 600s',output.getvalue())
        self.assertEqual(result['next_check_seconds'],600)

    def test_pending_audio_and_missing_completion_receipts_have_progress_logs(self):
        now=Time();clock=initiative.Clock(now,lambda *_:9,source='phone');output=io.StringIO()
        with redirect_stdout(output):
            clock.start();clock.heard();clock.speaking('voice');now.advance(10)
            clock.monitor(playback_status={'_job_id':'voice','done':False,'chunks':[{}]})
            now.advance(1)
            clock.monitor(playback_status={'_job_id':'voice','done':True,'chunks':[{},{}]})
        self.assertTrue(initiative.flush_console())
        self.assertIn('audio generation is still pending',output.getvalue())
        self.assertIn('awaiting client delivery receipt',output.getvalue())
        self.assertIn('silence clock is not armed yet',output.getvalue())
        self.assertEqual(clock.job,'voice');self.assertIsNone(clock.due)


if __name__ == "__main__":
    unittest.main()
