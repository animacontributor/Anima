"""Real Android foreground-service test; use only an isolated debug emulator.

Run after installing AnimaPhone-test.apk on emulator-5582. Models are stubbed,
the PC API is real, and all JavaScript is paused during the locked-screen phase.
"""
import base64
import io
from pathlib import Path
import subprocess
import sys
import threading
import time
import wave
from unittest.mock import patch

from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'engine'));sys.path.insert(0,str(ROOT/'tests'))
from test_phone import ApiTests
import config
import voice_initiative

ADB=ROOT/'.runtime/android-build/sdk/platform-tools/adb.exe'
SERIAL='emulator-5582'

def adb(*args):
    return subprocess.check_output([str(ADB),'-s',SERIAL,*args],text=True,timeout=20).strip()

def wait(predicate,seconds=20):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        try:
            if predicate():return
        except subprocess.CalledProcessError:
            pass
        time.sleep(.1)
    raise AssertionError('Screen-off condition did not become true before timeout')

def main():
    fixture=ApiTests('test_text_chat_uses_same_core_and_physicalize_is_fixed_before_start');fixture.setUp()
    stopped=threading.Event();decisions=[]
    original=fixture.worker.request
    def worker(op,**data):
        if op not in ('synthesize','synthesize_stream'):return original(op,**data)
        buffer=io.BytesIO()
        with wave.open(buffer,'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(b'\0\0'*20000)
        packet={'audio':base64.b64encode(buffer.getvalue()).decode(),'seconds':1.25}
        if op=='synthesize_stream':data['on_audio'](packet);return {'seconds':1.25}
        return packet
    def decide(history,elapsed,mode="phone_voice",**kwargs):
        text=f'Locked screen passage {len(decisions)+1}.';decisions.append((time.monotonic(),text))
        return {'text':text,'next_check_seconds':3,'wait_for_user':len(decisions)>=3},[
            {'role':'user','content':'private quiet event','_engine':True},{'role':'assistant','content':text}]
    def ticker():
        while not stopped.wait(.1):fixture.house.tick()
    browser=None;cdp=None
    try:
        adb('shell','am','force-stop','local.anima.phone')
        adb('shell','input','keyevent','224');adb('shell','wm','dismiss-keyguard')
        adb('shell','am','start','-n','local.anima.phone/.MainActivity')
        wait(lambda:bool(adb('shell','pidof','local.anima.phone')))
        pid=adb('shell','pidof','local.anima.phone').split()[0]
        adb('forward','tcp:9234','localabstract:webview_devtools_remote_'+pid)
        with patch.object(fixture.worker,'request',side_effect=worker),patch.object(voice_initiative,'decide',side_effect=decide):
            with sync_playwright() as p:
                for attempt in range(20):
                    try:browser=p.chromium.connect_over_cdp('http://127.0.0.1:9234',no_defaults=True);break
                    except Exception:time.sleep(.2)
                assert browser is not None,'Debug WebView unavailable'
                page=browser.contexts[0].pages[0];page.wait_for_selector('#address')
                page.wait_for_function('!phoneApp.pairing')
                port=fixture.server.server_port
                page.fill('#address','http://10.0.2.2:'+str(port));page.fill('#pairCode',fixture.house.pairing.data['code'])
                page.click('#connect');page.wait_for_selector('#startVisit',state='visible');page.click('#startVisit')
                page.wait_for_selector('#voiceToggle',state='visible');page.click('#voiceToggle')
                page.wait_for_function('phoneApp.voice.enabled && !phoneApp.voice.starting',timeout=30000)
                assert page.evaluate('phoneApp.voice.nativeCapture && phoneApp.voice.serverInitiative')
                clock=fixture.house.session.initiative;clock.gaussian=lambda *_:5
                fixture.house.session.history=[{'role':'user','content':'Talk me to sleep.','_system':'Fixture identity.'},
                                               {'role':'assistant','content':'Let me begin a story.'}]
                cdp=browser.contexts[0].new_cdp_session(page);cdp.send('Debugger.enable');cdp.send('Debugger.pause')
                adb('shell','input','keyevent','223')
                def screen_off():
                    power=adb('shell','dumpsys','power')
                    return 'mWakefulness=Asleep' in power or 'mWakefulness=Dozing' in power or 'state=OFF' in power
                wait(screen_off,5)
                clock.heard();clock.speaking('seed');clock.finished('seed')
                driver=threading.Thread(target=ticker,daemon=True);driver.start()
                wait(lambda:len(decisions)>=3,35)
                wait(lambda:clock.job is None and clock.wait_for_user,10)
                # The stub's spoken rows are visible but have no _initiative tag.
                delivered=[row for row in fixture.house.session.history if row.get('content','').startswith('Locked screen passage')]
                assert len(delivered)==3
                assert all('Playback completed' in row.get('_voice_delivery','') for row in delivered),delivered
                adb('shell','run-as','local.anima.phone','am','startservice','--user','0','-n','local.anima.phone/.VoiceService','-a','local.anima.phone.STOP_VOICE')
                wait(lambda:not fixture.house.voice,5)
                print('PASS: three server decisions, actual native AudioTrack completion receipts, and notification Stop while the screen is off and JavaScript is paused.')
    finally:
        stopped.set()
        try:adb('shell','run-as','local.anima.phone','am','startservice','--user','0','-n','local.anima.phone/.VoiceService','-a','local.anima.phone.STOP_VOICE')
        except Exception:pass
        if cdp:
            try:cdp.send('Debugger.resume')
            except Exception:pass
        adb('shell','input','keyevent','224')
        fixture.tearDown()

if __name__=='__main__':main()
