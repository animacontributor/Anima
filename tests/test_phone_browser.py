"""Phone UI -> real LAN API with a fake native bridge, microphone and models."""
import base64
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_phone import ApiTests


ROOT=Path(__file__).resolve().parent.parent
OUT=ROOT/'.runtime/phone-app/previews';OUT.mkdir(parents=True,exist_ok=True)

def main():
    fixture=ApiTests('test_text_chat_uses_same_core_and_physicalize_is_fixed_before_start')
    fixture.setUp()
    class Assets(SimpleHTTPRequestHandler):
        def __init__(self,*a,**k):super().__init__(*a,directory=str(ROOT/'android/assets'),**k)
        def log_message(self,*a):pass
    assets=ThreadingHTTPServer(('127.0.0.1',0),Assets)
    thread=threading.Thread(target=assets.serve_forever,daemon=True);thread.start()
    errors=[]
    def rpc(route,method,body):
        data=body.encode() if body else None
        req=urllib.request.Request(fixture.base+route,data=data,method='GET' if method=='BINARY' else method,
            headers={'Authorization':'Bearer '+fixture.token})
        try:
            with urllib.request.urlopen(req,timeout=10) as response:
                raw=response.read()
                return {'audio':base64.b64encode(raw).decode(),'mime':response.headers.get('Content-Type')} if method=='BINARY' else json.loads(raw)
        except urllib.error.HTTPError as e:
            try:
                result=json.load(e);result['_status']=e.code;return result
            finally:e.close()
    def upload(visit):
        req=urllib.request.Request(fixture.base+'/api/upload',data=b'A fixture note.',headers={
            'Authorization':'Bearer '+fixture.token,'X-Visit':visit,'X-Filename':'a-note.txt'})
        with urllib.request.urlopen(req) as response:return json.load(response)
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,args=['--autoplay-policy=no-user-gesture-required'])
            page=browser.new_page(viewport={'width':390,'height':844},device_scale_factor=1)
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.expose_function('testRpc',rpc);page.expose_function('testUpload',upload)
            page.add_init_script("""
              window.AnimaNative={
                configuration:()=>JSON.stringify({address:'http://192.168.1.20:8780',paired:false}),
                pair:(id,address,code)=>window.nativeReply(id,{name:'Friend'}),
                request:(id,route,method,body)=>window.testRpc(route,method,body).then(r=>window.nativeReply(id,r)),
                startMicrophone:id=>window.nativeReply(id,{ok:true}),stopMicrophone:()=>{},forget:()=>{},closeApp:()=>{},
                configureAudio:(id,settings)=>window.nativeReply(id,{ok:true}),audioDevices:id=>window.nativeReply(id,{inputs:[{id:-1,label:'Bluetooth microphone (automatic, prefer LE Audio)'},{id:1,label:'Phone microphone'}],echoAvailable:true,echoEnabled:false}),
                chooseFiles:visit=>{window.appPaused();window.testUpload(visit).then(r=>{window.fileUploaded(visit,r);window.filePickerFinished();window.appResumed();});}
              };
            """)
            page.goto('http://127.0.0.1:'+str(assets.server_port))
            page.screenshot(path=str(OUT/'connect.png'))
            page.fill('#pairCode','ABCD-EFGH');page.click('#connect');page.wait_for_selector('#readyCard',state='visible')
            page.check('#physicalize');page.screenshot(path=str(OUT/'start.png'),animations='disabled')
            page.click('#audioBefore');page.wait_for_selector('#audio_input option[value="1"]',state='attached')
            assert page.input_value('#audio_mode')=='speaker'
            page.select_option('#audio_input','-1')
            assert page.input_value('#audio_mode')=='headphones'
            assert page.evaluate("JSON.parse(localStorage.phoneAudio).input")==-1
            page.evaluate("phoneApp.audioSettings.route({route:'Bluetooth LE microphone — Test headset',verified:true,requestedMatched:true,bluetoothMode:'LE Audio',echoAvailable:true})")
            assert 'Verified microphone: Bluetooth LE microphone' in page.inner_text('#audioRoute')
            assert 'LE Audio' in page.inner_text('#audioRoute')
            page.evaluate("async()=>{phoneApp.audioSettings.value.input=99;await phoneApp.audioSettings.devices();}")
            assert page.input_value('#audio_input')=='99'
            assert page.evaluate('phoneApp.audioSettings.value.input')==99
            page.select_option('#audio_mode','speaker')
            page.select_option('#audio_input','1');page.fill('#audio_sensitivity','75');page.locator('#audio_sensitivity').dispatch_event('input')
            assert page.evaluate('phoneApp.audioSettings.value.sensitivity')==75
            for control,value in [('speed','85'),('whisper','35'),('pause','260')]:
                page.fill('#audio_'+control,value);page.locator('#audio_'+control).dispatch_event('input')
            preview=page.evaluate('async()=>{await phoneApp.previewDelivery();return phoneApp.deliveryPreview.active?.source.buffer.numberOfChannels;}')
            assert preview==1,preview
            assert not fixture.turns
            assert fixture.worker.calls[-1][1]['delivery']=={'speed':.85,'whisper_mix':.35,'pause_ms':260,'guidance':2}
            page.screenshot(path=str(OUT/'audio-settings.png'),animations='disabled');page.click('#closeAudio')
            page.click('#positionBefore');page.click('[data-position="left"]')
            page.fill('#soundHeight','25');page.locator('#soundHeight').dispatch_event('input')
            assert page.evaluate('window.phoneApp.spatial.state.enabled')
            page.click('#applyPosition');page.wait_for_function("document.getElementById('positionStatus').textContent.includes('Applied')")
            page.click('#previewPosition');page.wait_for_function('phoneApp.spatial.previewSource!==null')
            page.click('#previewPosition');page.wait_for_function('phoneApp.spatial.previewSource===null')
            page.screenshot(path=str(OUT/'spatial.png'),animations='disabled');page.click('#closePosition')
            page.click('#startVisit');page.wait_for_selector('#room',state='visible')
            assert page.inner_text('#presenceLabel')=='Sharing the same room'
            page.fill('#message','Hello from my phone.');page.click('#send')
            page.wait_for_selector('.message.her');assert fixture.turns[-1][1]['physicalize'] is True
            page.click('#share');page.wait_for_selector('.message.file')
            page.fill('#message','Have a look at the note.');page.click('#send')
            page.wait_for_function("document.querySelectorAll('.message.her').length===2")
            assert 'shared/books/' in fixture.turns[-1][0]
            page.click('#voiceToggle');page.wait_for_function("window.phoneApp.voice.enabled && !window.phoneApp.voice.starting")
            assert page.evaluate("window.phoneApp.spatial.desktopRendered && window.phoneApp.spatial.panner===null")
            page.evaluate("""() => {
                function frame(value){let raw='';for(let i=0;i<320;i++)raw+=String.fromCharCode(value&255,(value>>8)&255);return btoa(raw);}
                const speech=frame(5000),quiet=frame(0);
                for(let i=0;i<20;i++)window.microphoneFrame(speech);
                for(let i=0;i<55;i++)window.microphoneFrame(quiet);
            }""")
            page.wait_for_function("document.querySelectorAll('.message.her').length===3")
            page.wait_for_function("window.phoneApp.voice.job===null && !window.phoneApp.voice.draining")
            assert fixture.turns[-1][1]['mode']=='phone_voice'
            assert next(data for op,data in fixture.worker.calls if op=='synthesize_stream')['delivery']['whisper_mix']==.35
            assert 'That is Squadron 42.' in page.inner_text('#messages')
            page.screenshot(path=str(OUT/'conversation.png'))
            overflow=page.evaluate("document.documentElement.scrollWidth>window.innerWidth")
            assert not overflow
            page.evaluate('window.appPaused(true)');assert page.evaluate('window.phoneApp.voice.enabled')
            page.evaluate('window.appResumed()')
            page.evaluate('window.backgroundVoiceStopped()');page.wait_for_function('!window.phoneApp.voice.enabled')
            page.wait_for_function('!window.phoneApp.voice.stopping')
            assert fixture.house.visit
            page.evaluate('window.appResumed()');page.click('#leave');page.wait_for_selector('#setup',state='visible')
            assert not errors,errors
            spatial=page.evaluate("""async()=>{
              async function render(azimuth){
                const c=new OfflineAudioContext(2,16000,48000),buffer=c.createBuffer(1,12000,48000),data=buffer.getChannelData(0);
                let seed=42;for(let i=0;i<data.length;i++){seed=(Math.imul(seed,1664525)+1013904223)>>>0;data[i]=(seed/4294967296-.5)*.1;}
                const source=c.createBufferSource();source.buffer=buffer;const g=c.createGain();source.connect(g);
                PhoneSpatial.route(c,g,{enabled:true,azimuth,elevation:0,distance:.5});source.start();const out=await c.startRendering();
                const energy=channel=>{let sum=0;for(const v of out.getChannelData(channel))sum+=v*v;return sum;};
                return [energy(0),energy(1)];
              }
              return {left:await render(-90),right:await render(90)};
            }""")
            assert spatial['left'][0]>spatial['left'][1]*1.2,spatial
            assert spatial['right'][1]>spatial['right'][0]*1.2,spatial
            (OUT/'spatial-render.json').write_text(json.dumps(spatial,indent=2))
            browser.close()
        print('PASS: pairing, physicalize, text, files, voice, pause/resume, leave and actual HRTF left/right rendering.')
    finally:
        assets.shutdown();assets.server_close();thread.join(3);fixture.tearDown()

if __name__=='__main__':main()
