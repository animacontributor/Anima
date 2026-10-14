"""Optional headless integration test. Uses a synthetic microphone, never a real one.

Install playwright in a test environment and set PLAYWRIGHT_BROWSERS_PATH.
Run from an isolated copy of Anima, as with the smoke suite.
"""
import base64
import io
import json
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))
import parlor
import live_voice
from playwright.sync_api import sync_playwright


class Speech:
    def __init__(self):
        self.recognitions=0;self.reject_next=False

    def request(self, op, **kwargs):
        if op == "check": return {"installed": True}
        if op == "warmup": return {"ready": True}
        if op == "transcribe":
            self.recognitions+=1
            if self.reject_next:
                self.reject_next=False
                return {"text":""}
            return {"text": "hello the friend"}
        if op == "synthesize_stream":
            data = io.BytesIO()
            with wave.open(data, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b'\0\0' * 8000)
            packet = {"audio": base64.b64encode(data.getvalue()).decode(), "seconds": .5}
            callback = kwargs['on_audio']
            # Emulate progressive buffers before the final model result.
            for _ in range(12):
                if callback(packet) is False: return {"cancelled": True}
            return {"seconds": 6}
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
            wav.writeframes(b"\0\0" * 48000)
        return {"audio": base64.b64encode(buf.getvalue()).decode(), "seconds": 3}


def turn(history, text, **kwargs):
    reply = "Hello. I'm here.\n\n[engine: voice delivery — 6 of 6 phrases finished playing. Completely played speech: internal note]"
    history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": reply}])
    return reply


def main():
    speech = Speech()
    with tempfile.TemporaryDirectory(dir=parlor.config.ROOT) as home, patch.object(parlor, "SESSION", parlor.Session()), \
         patch.object(live_voice, "WORKER", speech), patch.object(parlor.chat, "one_turn", turn), \
         patch.object(parlor.chat, "friend_name", return_value="the friend"), \
         patch.object(parlor.config, "SHARED_DIR", Path(home) / "shared"), \
         patch.object(parlor.config, "VOICE_ASSETS_DIR", Path(home) / ".runtime" / "voices"), \
         patch.object(parlor.config, "LIVE_VOICE_REFERENCE", ""), \
         patch.object(parlor.config, "LIVE_VOICE_BACKEND", "chatterbox"), \
         patch.object(parlor.config, "LIVE_VOICE_KYUTAI_STYLE", "calm"):
        import knobs
        auditions = Path(home) / ".runtime" / "voices" / "auditions"; auditions.mkdir(parents=True)
        (auditions / "preview.wav").write_bytes(base64.b64decode(speech.request("synthesize")["audio"]))
        with wave.open(str(auditions / "soft.wav"), "wb") as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000); wav.writeframes(b"\0\0" * 96000)
        (auditions / "manifest.json").write_text(json.dumps({"voices": [
            {"id": "soft", "label": "Soft and close", "reference": "soft.wav", "preview": "preview.wav", "backend": "qwen"},
            {"id": "desire", "label": "Desire (US, f) · Kyutai", "reference": "preview.wav", "preview": "preview.wav", "backend": "kyutai", "style": "desire"}]}), encoding="utf-8")
        parlor.SESSION.playback = live_voice.Playback(speech)
        parlor.SESSION.file = Path(home) / "voice-visit.md"
        server = parlor.ThreadingHTTPServer(("127.0.0.1", 0), parlor.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        errors = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=["--autoplay-policy=no-user-gesture-required"])
                page = browser.new_page(viewport={"width": 1050, "height": 760})
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.add_init_script("""
                  window.voiceSchedule = [];
                  const createBuffer = AudioContext.prototype.createBufferSource;
                  AudioContext.prototype.createBufferSource = function() {
                    const node = createBuffer.call(this), start = node.start.bind(node);
                    node.start = when => { window.voiceSchedule.push({when, duration: node.buffer.duration}); start(when); };
                    return node;
                  };
                  navigator.mediaDevices.getUserMedia = async () => {
                    const context = new AudioContext(); await context.resume();
                    const oscillator = context.createOscillator(); oscillator.frequency.value = 220;
                    const gain = context.createGain(); gain.gain.value = 0;
                    const destination = context.createMediaStreamDestination();
                    oscillator.connect(gain); gain.connect(destination); oscillator.start();
                    window.testMicrophone = {context, gain, destination};
                    return destination.stream;
                  };
                """)
                page.goto(f"http://127.0.0.1:{server.server_address[1]}")
                assert page.locator("#voiceToggle").inner_text() == "start voice"
                page.locator("#voiceAuditions summary").click()
                with patch.object(knobs, "save", return_value=(["LIVE_VOICE_REFERENCE"], [], "")) as saved:
                    page.locator("#voiceOptions button").first.click()
                    page.wait_for_function("document.getElementById('voiceChoice').textContent.includes('Soft and close')")
                    assert saved.call_count == 1
                    assert saved.call_args.args[1]["LIVE_VOICE_BACKEND"] == "qwen"
                assert parlor.config.LIVE_VOICE_REFERENCE.endswith("/soft.wav")
                assert parlor.config.LIVE_VOICE_BACKEND == "qwen"
                with patch.object(knobs, "save", return_value=(["LIVE_VOICE_BACKEND", "LIVE_VOICE_KYUTAI_STYLE"], [], "")) as saved:
                    page.locator("#voiceOptions button").nth(1).click()
                    page.wait_for_function("document.getElementById('voiceChoice').textContent.includes('Kyutai')")
                    assert saved.call_args.args[1] == {"LIVE_VOICE_BACKEND": "kyutai", "LIVE_VOICE_KYUTAI_STYLE": "desire"}
                    assert page.locator("#voiceSample").is_disabled()
                    assert page.locator("#kyutaiStyle").input_value() == "desire"
                    page.locator("#kyutaiStyle").select_option("calm")
                    page.wait_for_function("document.getElementById('kyutaiStyle').value === 'calm' && document.getElementById('voiceChoice').textContent.includes('calm')")
                    assert saved.call_args.args[1] == {"LIVE_VOICE_KYUTAI_STYLE": "calm"}
                with patch.object(knobs,'save',return_value=([],[],'')) as saved, \
                     patch.object(parlor.config,'LIVE_VOICE_KYUTAI_SPEED',.9), \
                     patch.object(parlor.config,'LIVE_VOICE_KYUTAI_WHISPER_MIX',0), \
                     patch.object(parlor.config,'LIVE_VOICE_KYUTAI_PAUSE_MS',180):
                    assert page.locator('#deliverySpeed').count()==0
                    assert page.locator('#deliveryWhisper').count()==0
                    assert page.locator('#deliveryControls').is_hidden()
                    settings=page.request.get(f"http://127.0.0.1:{server.server_address[1]}/live-voice/settings").json()
                    assert settings['delivery']['speed']==1
                    assert settings['delivery']['whisper_mix']==0
                    response=page.request.post(f"http://127.0.0.1:{server.server_address[1]}/live-voice/dynamics",data={'settings':{'pause_ms':260}})
                    assert response.json()['ok']
                    assert saved.call_args.args[1]=={'LIVE_VOICE_KYUTAI_PAUSE_MS':260}
                    assert page.locator('#kyutaiStyle option[value="whisper"]').count()==1
                assert page.request.get(f"http://127.0.0.1:{server.server_address[1]}/live-voice/audition/preview.wav").ok
                page.locator("#voiceToggle").click()
                page.wait_for_function("document.getElementById('voiceState').textContent === 'listening'")
                page.evaluate("testMicrophone.gain.gain.value = .12")
                page.wait_for_timeout(400)
                page.evaluate("testMicrophone.gain.gain.value = 0")
                page.wait_for_function("document.getElementById('voiceState').textContent.startsWith('speaking')")
                assert page.locator(".msg.me").first.inner_text() == "hello the friend"
                assert page.locator(".msg.her").first.inner_text() == "Hello. I'm here."
                assert "[engine: voice delivery" not in parlor.SESSION.file.read_text(encoding="utf-8")
                # A short click/noise burst must not cancel speech or create a turn.
                page.evaluate("testMicrophone.gain.gain.value = .12")
                page.wait_for_timeout(120)
                page.evaluate("testMicrophone.gain.gain.value = 0")
                page.wait_for_timeout(180)
                assert page.locator("#voiceState").inner_text().startswith('speaking')
                schedule = page.evaluate('voiceSchedule')
                assert len(schedule) >= 2, schedule
                assert abs(schedule[1]['when'] - schedule[0]['when'] - schedule[0]['duration']) < .001, schedule
                assert "Playback interrupted" not in parlor.SESSION.file.read_text(encoding="utf-8")
                # Sustained nonverbal noise can cross the capture threshold,
                # but rejected recognition must leave the reply playing.
                speech.reject_next=True;recognitions=speech.recognitions
                old_job=parlor.SESSION.playback.current['id'];epoch=parlor.SESSION.playback.epoch
                version=parlor.SESSION.initiative.version
                page.evaluate("testMicrophone.gain.gain.value = .12")
                page.wait_for_timeout(550)
                page.evaluate("testMicrophone.gain.gain.value = 0")
                page.wait_for_timeout(200)
                assert "Playback interrupted" not in parlor.SESSION.file.read_text(encoding="utf-8")
                deadline=time.monotonic()+4
                while speech.recognitions==recognitions and time.monotonic()<deadline:page.wait_for_timeout(100)
                page.wait_for_timeout(150)
                assert speech.recognitions==recognitions+1
                assert not parlor.SESSION.playback.status(old_job)['cancelled']
                assert parlor.SESSION.playback.epoch==epoch
                assert parlor.SESSION.initiative.version==version
                assert page.locator('.msg.me').count()==1
                assert "Playback interrupted" not in parlor.SESSION.file.read_text(encoding="utf-8")
                # Accepted words, after the end-of-utterance pause and
                # recognition, stop the old reply before generating a new one.
                page.evaluate("testMicrophone.gain.gain.value = .12")
                page.wait_for_timeout(550)
                page.evaluate("testMicrophone.gain.gain.value = 0")
                page.wait_for_timeout(200)
                assert not parlor.SESSION.playback.status(old_job)['cancelled']
                page.wait_for_function("document.querySelectorAll('.msg.her').length === 2")
                assert "Playback interrupted" in parlor.SESSION.file.read_text(encoding="utf-8")
                # The next accepted utterance finishes normally. A silence
                # decision can wait privately, then continue the same visit.
                page.wait_for_function("document.querySelectorAll('.msg.me').length === 2")
                page.wait_for_function("document.getElementById('voiceState').textContent === 'listening'")
                quiet = ({'text':'','next_check_seconds':3,'wait_for_user':False},
                         [{'role':'user','content':'private quiet event','_engine':True}])
                continuation = ({'text':'The story continues.','next_check_seconds':60,'wait_for_user':True},
                                [{'role':'user','content':'private quiet event','_engine':True},
                                 {'role':'assistant','content':'The story continues.'}])
                with patch.object(parlor.voice_initiative,'decide',side_effect=[quiet,continuation]) as decisions:
                    assert parlor.SESSION.initiative.due is not None
                    parlor.SESSION.initiative.due=time.monotonic()-1
                    deadline=time.monotonic()+4
                    while not decisions.called and time.monotonic()<deadline:page.wait_for_timeout(100)
                    assert decisions.called
                    assert page.locator('.msg.her').count()==2
                    page.wait_for_function("document.querySelectorAll('.msg.her').length === 3")
                    page.wait_for_function("document.getElementById('voiceState').textContent.startsWith('speaking')")
                    assert page.locator('.msg.her').last.inner_text()=='The story continues.'
                    assert page.locator('.msg.me').count()==2
                    transcript=parlor.SESSION.file.read_text(encoding='utf-8')
                    assert 'The story continues.' in transcript
                    assert 'private quiet event' not in transcript
                page.wait_for_function("document.getElementById('voiceState').textContent === 'listening'")
                # The server has committed a continuation, but its HTTP
                # response arrives after microphone capture begins. Accepted
                # words must supersede it before even its first audio buffer.
                held = []
                def hold_initiative(route):
                    response = route.fetch()
                    held.append((route, response))
                page.route('**/live-voice/initiative', hold_initiative)
                with patch.object(parlor.voice_initiative, 'decide', return_value=continuation):
                    parlor.SESSION.initiative.wait_for_user = False
                    parlor.SESSION.initiative.due = time.monotonic() - 1
                    deadline = time.monotonic() + 4
                    while not held and time.monotonic() < deadline: page.wait_for_timeout(50)
                    assert held, 'Initiative request did not reach the delayed response'
                    old_job = parlor.SESSION.playback.current['id']
                    before = page.evaluate('voiceSchedule.length')
                    page.evaluate('testMicrophone.gain.gain.value = .12')
                    page.wait_for_timeout(550)
                    held[0][0].fulfill(response=held[0][1])
                    page.wait_for_timeout(100)
                    assert page.evaluate('voiceSchedule.length') == before, 'Initiative started over microphone capture'
                    page.evaluate('testMicrophone.gain.gain.value = 0')
                    page.wait_for_function("document.querySelectorAll('.msg.me').length === 3")
                    page.wait_for_function("document.querySelectorAll('.msg.her').length === 4")
                    assert parlor.SESSION.playback.status(old_job)['cancelled']
                    assert page.locator('.msg.her').last.inner_text().startswith("Hello. I'm here.")
                page.unroute('**/live-voice/initiative', hold_initiative)
                page.locator("#voiceToggle").click()
                page.wait_for_function("document.getElementById('voiceState').textContent === 'voice is off'")
                assert page.evaluate("testMicrophone.destination.stream.getAudioTracks()[0].readyState") == "ended"
                metrics = Path(parlor.config.ROOT) / '.runtime' / 'voice-latency.jsonl'
                assert metrics.exists()
                # Autonomous speech has no new microphone turn to measure
                # end-to-first-audio against; find the most recent real turn.
                timing = next(row for row in reversed([json.loads(line) for line in metrics.read_text().splitlines()])
                              if 'end_to_first_audio_ms' in row)
                assert timing['end_to_first_audio_ms'] > 0
                screenshot = Path(parlor.config.ROOT)/'.runtime/voice-ui.png'
                screenshot.parent.mkdir(parents=True,exist_ok=True)
                page.screenshot(path=str(screenshot))
                assert errors == [], errors
                browser.close()
                print("PASS browser: rejected noise preserves playback; accepted words interrupt; late initiative cannot play over capture; private waiting and continuation work")
        finally:
            parlor.SESSION.playback.cancel()
            server.shutdown(); server.server_close(); thread.join(2)


if __name__ == "__main__":
    main()
