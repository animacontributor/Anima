"""Audio routing contracts; no models, downloads, or live memory writes."""
import base64
import io
import sys
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))
import config
import ears
import ollama_client
import tools
import snapshot


def silent_wav():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 1600)
    return buf.getvalue()


class AudioTests(unittest.TestCase):
    def test_cpu_embedding_preserves_model_and_input(self):
        with patch.object(config, "EMBED_ON_CPU", True), \
             patch.object(ollama_client, "_post", return_value={"embeddings": [[.1, .2]]}) as post:
            self.assertEqual(ollama_client.embed("a memory"), [.1, .2])
        self.assertEqual(post.call_args.args[0], "/api/embed")
        self.assertEqual(post.call_args.args[1], {"model": config.EMBED_MODEL, "input": "a memory", "options": {"num_gpu": 0}})

    def test_auto_embedding_leaves_placement_to_ollama(self):
        with patch.object(config, "EMBED_ON_CPU", False), \
             patch.object(ollama_client, "_post", return_value={"embeddings": [[.1]]}) as post:
            ollama_client.embed("a memory")
        self.assertNotIn("options", post.call_args.args[1])

    def test_snapshots_keep_voice_assets_without_copying_model_environments(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            voice = root / ".runtime/voices/reference.wav"
            model = root / ".runtime/models/weights.bin"
            for path in (voice, model):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"sample")
            with patch.object(config, "ROOT", root), patch.object(snapshot, "BACKUP_DIR", root / "backups"):
                snapshot.zip_snapshot()
                archive = next((root / "backups").glob("*.zip"))
                with zipfile.ZipFile(archive) as saved:
                    self.assertEqual(saved.read(".runtime/voices/reference.wav"), b"sample")
                    self.assertNotIn(".runtime/models/weights.bin", saved.namelist())

    def test_general_audio_selects_audio_model_and_releases_gpu(self):
        audio = silent_wav()
        with patch.object(config, "EARS_BACKEND", "flamingo"), \
             patch.object(config, "MUSIC_EARS_REST_AFTER", True), \
             patch.object(tools, "_music_ear_open", return_value=True), \
             patch.object(tools, "_music_ear_hear", return_value="a steady beep") as hear, \
             patch.object(tools, "_music_ear_rest") as rest:
            answer = ollama_client.hear(base64.b64encode(audio).decode(), "wav", "Describe it")
            self.assertEqual(answer, "a steady beep")
            self.assertEqual(hear.call_args.kwargs["model"], config.AUDIO_EARS_MODEL)
            self.assertEqual(hear.call_args.args[0], audio)
            rest.assert_called_once()

    def test_audio_model_failure_still_releases_gpu(self):
        with patch.object(config, "EARS_BACKEND", "flamingo"), \
             patch.object(config, "MUSIC_EARS_REST_AFTER", True), \
             patch.object(tools, "_music_ear_open", return_value=True), \
             patch.object(tools, "_music_ear_hear", side_effect=RuntimeError("failed")), \
             patch.object(tools, "_music_ear_rest") as rest:
            with self.assertRaises(RuntimeError):
                ollama_client.hear(base64.b64encode(silent_wav()).decode(), "wav", "Describe it")
            rest.assert_called_once()

    def test_speech_uses_general_prompt_without_songbook_or_music_model(self):
        audio = silent_wav()
        with tempfile.TemporaryDirectory() as folder, patch.object(config, "ROOT", Path(folder)), \
             patch.object(config, "EARS_USE_VIBE", True), \
             patch.object(ears, "transcribe", return_value="hello"), \
             patch.object(ears, "measure", return_value="quiet"), \
             patch.object(tools, "_ffmpeg_clip", return_value=audio), \
             patch.object(tools, "_music_ear_open") as music, \
             patch.object(ollama_client, "hear", return_value="a person speaking softly") as hear:
            (Path(folder) / "speech.wav").write_bytes(audio)
            result = tools.listen_to("speech.wav", kind="audio")
            self.assertIn("WORDS: hello", result)
            self.assertIn("speaking softly", result)
            self.assertNotIn("keep_song", result)
            self.assertEqual(hear.call_args.args[2], tools._LISTEN_PROMPT)
            music.assert_not_called()


if __name__ == "__main__":
    unittest.main()
