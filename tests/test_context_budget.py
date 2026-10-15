"""Voice context contracts. Run in an isolated test home, like the voice suite."""
import copy
from datetime import date
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))
import assemble
import chat
import config
import context_budget as budget
import ollama_client
import parlor
import tools
import voice_initiative


def visit(exchanges=9):
    turns = []
    for n in range(exchanges):
        turns.extend([{"role": "user", "content": f"message {n}", "_moment": f"memory {n}",
                       "_surfaced": [n], "_prompt": 110000},
                      {"role": "assistant", "content": f"answer {n}"}])
    turns[0].update(_system="identity", _system_day=date.today().isoformat())
    return turns


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in {
            "ROOT": self.root, "MEMORY_DIR": self.root / "memory", "EPISODIC_DIR": self.root / "episodes",
            "FOLD_KEEP_TURNS": 12, "NUM_CTX": 131072, "FOLD_AT": .8, "FOLD_AFTERGLOW": False,
            "CONTEXT_GUARD": True, "CONTEXT_RESERVE_TOKENS": 4096, "CHAT_THINK": False,
        }.items():
            p = patch.object(config, name, value); p.start(); self.addCleanup(p.stop)
        config.MEMORY_DIR.mkdir(); config.EPISODIC_DIR.mkdir()
        for obj, name, value in [(assemble, "system_prompt", "fresh identity"),
                                  (tools, "current_definitions", []), (chat, "friend_name", "Fixture")]:
            p = patch.object(obj, name, return_value=value); p.start(); self.addCleanup(p.stop)
        tools.fold_pending()

    def test_oversize_post_stops_before_network(self):
        with patch.object(ollama_client.urllib.request, "urlopen") as network:
            with self.assertRaises(budget.ContextFull):
                ollama_client._post("/api/chat", {"messages": [{"role": "user", "content": "x" * 500000}],
                                                "options": {"num_ctx": 131072}})
            network.assert_not_called()

    def test_budget_counts_schema_images_tool_calls_and_reserves(self):
        messages = [{"role": "system", "content": "identity"},
                    {"role": "user", "content": "hello", "images": ["not-an-image"]},
                    {"role": "tool", "content": "evidence"}]
        report = budget.check({"messages": messages, "tools": [{"description": "tool"}],
                               "options": {"num_ctx": 131072, "num_predict": 2048}})
        self.assertEqual(report["reserved_output"], 2048)
        self.assertEqual(report["estimated_parts"]["images"], 8192)
        self.assertGreater(report["estimated_parts"]["tool_results"], 0)
        self.assertGreater(report["estimated_parts"]["tool_schemas"], 0)
        self.assertNotIn("evidence", json.dumps(report))

    def test_counter_reporting_keeps_cached_distinct_from_total(self):
        msg = ollama_client._parse({"message": {"role": "assistant", "content": "Hello."},
            "prompt_eval_count": 15000, "prompt_eval_cached_count": 14800,
            "context_budget": {"estimated_input": 18000}, "eval_count": 10})
        tally = ollama_client.Spent(); tally.add(msg)
        self.assertEqual(tally.prompt, 15000)
        self.assertEqual(tally.cached, 14800)
        self.assertEqual(tally.budget["estimated_input"], 18000)

    def test_rejected_first_request_does_not_duplicate_user_input(self):
        history = visit(1); before = [t["content"] for t in history]
        with patch.object(assemble, "moment", return_value=("", [])), \
             patch.object(chat, "mark_visit_live"), patch.object(tools, "refresh_her_tools"), \
             patch.object(ollama_client, "chat", side_effect=budget.ContextFull("too large")):
            with self.assertRaises(budget.ContextFull):
                chat.one_turn(history, "unsent", mode="voice", on_event=lambda *a: None)
        self.assertEqual([t["content"] for t in history], before)

    def test_voice_retrieval_is_bounded_and_skipped_ids_remain_available(self):
        rows = [{"id": i, "kind": "note", "created": "2026-10-15", "text": "x" * size}
                for i, size in enumerate([5000, 90, 90, 90])]
        with patch.object(assemble.memory, "count", return_value=4), \
             patch.object(assemble.memory, "search", return_value=rows) as search, \
             patch.object(assemble.memory, "recent", return_value=[]), \
             patch.object(config, "VOICE_MEMORY_CHARS", 250):
            text, ids = assemble.retrieved_ids("topic", exclude={3}, voice=True)
        self.assertLessEqual(len(text), 250)
        self.assertEqual(ids, [1, 2])
        self.assertEqual(search.call_args.kwargs["top_k"], 10)

    def test_missing_summary_keeps_exact_history(self):
        history = visit(); before = copy.deepcopy(history)
        new, path, note = chat.fold_history(history, "", "12:00")
        self.assertIs(new, history); self.assertEqual(history, before)
        self.assertIn("retained", note)

    def test_failed_summary_does_not_leave_engine_bells(self):
        history = visit(); before = [t["content"] for t in history]
        with patch.object(ollama_client, "chat", return_value={"role": "assistant", "content": ""}):
            self.assertEqual(chat.fold_bell(history, 110000), {})
        self.assertEqual([t["content"] for t in history], before)
        self.assertFalse(any(t.get("_engine") for t in history))

    def test_fold_preserves_delivery_references_and_releases_dropped_memory_ids(self):
        history = visit(); tail = history[-1]
        tail["tokens"] = {"prompt": 120000, "reply": 12}
        new, _, _ = chat.fold_history(history, "Continuity, corrections and open requests.", "12:00")
        self.assertEqual(len(new), 12)
        self.assertIs(new[-1], tail)
        self.assertEqual(set(new[0]["_surfaced"]), set(range(3, 9)))
        self.assertEqual(chat.held_tokens(new), 0)
        tail["_voice_delivery"] = "completed"
        self.assertEqual(new[-1]["_voice_delivery"], "completed")

    def test_archive_failure_prevents_replacing_or_mutating_history(self):
        history = visit(); before = copy.deepcopy(history)
        with patch.object(chat, "save_transcript", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                chat.fold_history(history, "Account.", "12:00", path=self.root / "old.md")
        self.assertEqual(history, before)

    def test_new_transcript_failure_also_preserves_original_history(self):
        history = visit(); before = copy.deepcopy(history)
        with patch.object(chat, "save_transcript", side_effect=[self.root / "old.md", OSError("disk full")]):
            with self.assertRaises(OSError):
                chat.fold_history(history, "Account.", "12:00", path=self.root / "old.md")
        self.assertEqual(history, before)

    def test_oversize_rebuilt_prompt_is_not_committed(self):
        history = visit(); before = copy.deepcopy(history)
        with patch.object(assemble, "system_prompt", return_value="x" * 500000), \
             patch.object(chat, "save_transcript") as save:
            with self.assertRaises(budget.ContextFull):
                chat.fold_history(history, "Account.", "12:00", path=self.root / "old.md")
            save.assert_not_called()
        self.assertEqual(history, before)

    def test_private_waits_can_fold_even_with_only_one_exchange(self):
        history = visit(1)
        for n in range(4):
            history.extend([{"role": "user", "content": "quiet check", "_engine": True, "_voice_decision": True},
                {"role": "assistant", "content": "", "_engine": True, "_voice_decision": True,
                 "tokens": {"prompt": 115000, "reply": 20},
                 "tool_calls": [{"function": {"name": "voice_initiative", "arguments": {"action": "wait"}}}]},
                {"role": "tool", "content": "accepted", "tool_name": "voice_initiative",
                 "_engine": True, "_voice_decision": True}])
        self.assertTrue(chat.foldable(history))
        self.assertEqual(chat.held_tokens(history), 115020)
        new, _, _ = chat.fold_history(history, "We are keeping quiet together.", "12:00")
        self.assertEqual([t["content"] for t in new], ["message 0", "answer 0"])
        self.assertFalse(any(t.get("_voice_decision") for t in new))

    def test_session_pending_accounts_do_not_cross_threads(self):
        barrier = threading.Barrier(2); histories = [visit(1), visit(1)]
        @tools.session_folds
        def request(history, text):
            tools._fold_store().update(text=text, when="12:00")
            barrier.wait(2)
        threads = [threading.Thread(target=request, args=(histories[n], str(n))) for n in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(3); self.assertFalse(thread.is_alive())
        self.assertEqual(tools.fold_pending(histories[0])["text"], "0")
        self.assertEqual(tools.fold_pending(histories[1])["text"], "1")
        self.assertEqual(tools.fold_pending(), {})

    def test_resumed_session_does_not_consume_an_unrelated_global_account(self):
        history = visit(1); before = copy.deepcopy(history)
        tools._fold_pending.update(text="an autonomous wake", when="12:00")
        self.assertEqual(tools.fold_pending(history), {})
        self.assertEqual(history, before)
        self.assertEqual(tools.fold_pending()["text"], "an autonomous wake")

    def test_voice_session_folds_before_inference_and_playback(self):
        session = parlor.Session(); session.history = visit(); calls = []
        session.playback = Mock(); session.playback.start.return_value = "job"
        def reply(history, *a, **k):
            calls.append("reply")
            self.assertIn("_fold", history[0])
            history.extend([{"role": "user", "content": "new"}, {"role": "assistant", "content": "ready"}])
            return "ready"
        session.playback.start.side_effect = lambda *a: calls.append("play") or "job"
        with patch.object(chat, "fold_bell", side_effect=lambda *a, **k: calls.append("fold") or {"text": "account"}), \
             patch.object(chat, "one_turn", side_effect=reply):
            result = session.send("new", spoken=True)
        self.assertEqual(calls, ["fold", "reply", "play"])
        self.assertEqual(result["reply"], "ready")

    def test_pause_never_folds_during_active_playback(self):
        session = parlor.Session(); session.history = visit(); session.initiative.job = "playing"
        with patch.object(config, "REFLECT_AFTER_MIN", 0), patch.object(session, "fold_if_due") as fold:
            session.pause_if_due()
        fold.assert_not_called()

    def test_long_tool_result_is_archived_and_can_be_read_in_pages(self):
        text = "a" * 15000 + "TARGET" + "b" * 15000
        result = tools.context_excerpt(text, "read_web")
        self.assertLessEqual(len(result), 12000)
        path = next((config.MEMORY_DIR / "tool_results").glob("*.txt"))
        self.assertEqual(path.read_text(encoding="utf-8"), text)
        with patch.object(tools, "_read_tell", return_value=""):
            page = tools.read_file(path.relative_to(config.ROOT).as_posix(), offset=15000)
        self.assertIn("TARGET", page)
        self.assertIn("offset=25000", page)

    def test_failed_tool_archive_keeps_full_evidence(self):
        text = "completed action: " + "x" * 15000
        with patch.object(Path, "write_text", side_effect=OSError("disk full")):
            self.assertEqual(tools.context_excerpt(text, "write_creation"), text)

    def test_repeated_voice_visits_fold_without_dropping_recent_words(self):
        session = parlor.Session(); session._checkpoint = Mock(); session.playback = Mock()
        session.playback.start.return_value = "job"
        folds = []
        def brain(messages, tools=None, **kwargs):
            report = budget.check({"messages": messages, "tools": tools,
                                  "options": {"num_ctx": config.NUM_CTX, "num_predict": 256}})
            counts = {"prompt": report["estimated_input"], "reply": 240}
            if "to be FOLDED:" in messages[-1].get("content", ""):
                folds.append(1)
                return {"role": "assistant", "content": "", "tokens": counts,
                        "tool_calls": [{"function": {"name": "fold_visit", "arguments": {"text": "We keep our ongoing conversation and open requests."}}}]}
            return {"role": "assistant", "content": "A spoken response. " * 40, "tokens": counts}
        with patch.object(config, "NUM_CTX", 8192), patch.object(config, "CONTEXT_RESERVE_TOKENS", 512), \
             patch.object(config, "SAMPLING_OPTIONS", {"num_predict": 256}), \
             patch.object(config, "FOLD_KEEP_TURNS", 6), \
             patch.object(assemble, "moment", return_value=("", [])), \
             patch.object(chat, "mark_visit_live"), patch.object(tools, "refresh_her_tools"), \
             patch.object(voice_initiative, "voice_tools", return_value=[]), \
             patch.object(ollama_client, "chat", side_effect=brain):
            for n in range(100):
                result = session.send(f"Spoken message {n}", spoken=True)
                self.assertNotIn("error", result, result)
        self.assertGreater(len(folds), 2)
        self.assertIn("Spoken message 99", [t["content"] for t in session.history])
        self.assertTrue(list(config.EPISODIC_DIR.glob("*.md")))


if __name__ == "__main__":
    unittest.main()
