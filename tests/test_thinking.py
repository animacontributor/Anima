"""Thinking request contracts; no live model requests or memory writes."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import config
import ollama_client


class ThinkingTests(unittest.TestCase):
    def test_chat_setting_is_sent_explicitly_and_per_call_values_override_it(self):
        for enabled, override, expected in [(False, None, False), (True, None, True),
                                            (False, False, False), (True, False, False),
                                            (False, True, True), (True, True, True)]:
            with self.subTest(chat_think=enabled, think=override), \
                 patch.object(config, 'CHAT_THINK', enabled), \
                 patch.object(ollama_client, '_post', return_value={
                     'message': {'role': 'assistant', 'content': 'Hello.'}
                 }) as post:
                ollama_client._chat([{'role': 'user', 'content': 'Hello'}],
                                    think=override, think_retries=0)
                self.assertEqual(post.call_count, 1)
                self.assertIs(post.call_args.args[1]['think'], expected)


if __name__ == '__main__':
    unittest.main()
