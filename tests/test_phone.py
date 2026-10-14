"""Phone API and presence contracts, without a real model or personal memory writes."""
import base64
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request
import wave

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))
import assemble
import chat
import config
import phone
import voice_lock


def audio():
    b=io.BytesIO()
    with wave.open(b,'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes(b'\0\0'*1600)
    return base64.b64encode(b.getvalue()).decode()


class Worker:
    def __init__(self):
        self.lock=threading.Lock();self.calls=[];self.closed=False
    def request(self,op,on_audio=None,**kwargs):
        self.calls.append((op,kwargs))
        if op=='check':return {'installed':True}
        if op=='warmup':return {'ok':True}
        if op=='transcribe':return {'text':'That is Squadron 42.'}
        if op=='synthesize_stream':
            on_audio({'audio':audio(),'seconds':.1});return {}
        if op=='synthesize':return {'audio':audio(),'seconds':.1}
        raise AssertionError(op)
    def close(self):self.closed=True

class SpatialWorker:
    """No resources or DSP dependencies needed for API/client contract tests."""
    def __init__(self):self.calls=[]
    def installed(self):return True
    def request(self,op,**data):
        self.calls.append((op,data))
        if op in ('prepare','begin'):return {'channels':2,'rate':48000,'nearest_ear_cm':2,'settings':data['settings'],'engine':'fixture stereo renderer'}
        if op in ('process','finish','preview'):
            b=io.BytesIO()
            with wave.open(b,'wb') as f:f.setnchannels(2);f.setsampwidth(3);f.setframerate(48000);f.writeframes(b'\0'*6*4800)
            return {'audio':base64.b64encode(b.getvalue()).decode(),'seconds':.1}
        return {'ok':True}


class PairingTests(unittest.TestCase):
    def test_token_is_persisted_as_hash_and_pairing_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            p=phone.Pairing(Path(d));token=p.exchange(p.data['code'][:4]+'-'+p.data['code'][4:])
            self.assertNotIn(token,p.file.read_text());self.assertTrue(phone.Pairing(Path(d)).authenticate('Bearer '+token))
            with self.assertRaises(phone.Problem):p.authenticate('Bearer not-a-token')
    def test_bad_codes_are_rate_limited_and_unicode_is_rejected_cleanly(self):
        with tempfile.TemporaryDirectory() as d:
            p=phone.Pairing(Path(d))
            for _ in range(12):
                with self.assertRaises(phone.Problem) as error:p.exchange('♥')
                self.assertEqual(error.exception.status,401)
            with self.assertRaises(phone.Problem) as error:p.exchange(p.data['code'])
            self.assertEqual(error.exception.status,429)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        # Temp folders can have symlink or short-name aliases on macOS/Windows.
        self.root=Path(self.temp.name).resolve()
        self.patches=[]
        values={'ROOT':self.root,'MEMORY_DIR':self.root/'memory','EPISODIC_DIR':self.root/'memory/episodic',
                'SHARED_DIR':self.root/'shared','CREATIONS_DIR':self.root/'creations','LIVE_VOICE_BACKEND':'kyutai','AFTERGLOW':False}
        for name,value in values.items():
            p=patch.object(config,name,value);p.start();self.patches.append(p)
        for path in values.values():
            if isinstance(path,Path):path.mkdir(parents=True,exist_ok=True)
        for target,value in [(chat,'friend_name'),(chat,'mark_visit_live'),(chat,'rest_brain')]:
            p=patch.object(target,value,return_value='Friend' if value=='friend_name' else None);p.start();self.patches.append(p)
        self.worker=Worker();self.spatial=SpatialWorker();self.house=phone.House(phone.Pairing(self.root/'pairing'),worker=self.worker,spatial_worker=self.spatial)
        self.server=phone.make_server('127.0.0.1',0,self.house)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)
        self.token=self.request('/api/pair',{'code':self.house.pairing.data['code']},auth=False)['token']
        self.turns=[]
        def turn(history,text,**kwargs):
            self.turns.append((text,kwargs))
            t={'role':'user','content':text}
            if kwargs.get('physicalize'):t['_physicalize']=True
            if kwargs.get('physicalize_voice') is not None:t['_physicalize_voice']=kwargs['physicalize_voice']
            history.extend([t,{'role':'assistant','content':'A fixture reply.'}])
            return 'A fixture reply.'
        self.brain=patch.object(chat,'one_turn',side_effect=turn);self.brain.start()
    def tearDown(self):
        self.house.finish(reflect=False)
        deadline=time.time()+3
        while self.house.closing and time.time()<deadline:time.sleep(.01)
        self.server.shutdown();self.server.server_close();self.thread.join(3)
        self.brain.stop()
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()
    def request(self,path,body=None,auth=True,headers=None):
        h=dict(headers or {})
        if auth:h['Authorization']='Bearer '+self.token
        raw=None if body is None else json.dumps(body).encode()
        req=urllib.request.Request(self.base+path,data=raw,headers=h)
        try:
            with urllib.request.urlopen(req,timeout=5) as response:return json.load(response)
        except urllib.error.HTTPError as e:
            try:
                data=json.load(e);data['_status']=e.code;return data
            finally:e.close()
    def start(self,physical=False):
        result=self.request('/api/start',{'physicalize':physical});self.visit=result['visit'];return result
    def test_all_session_routes_require_pairing_and_reject_foreign_origins(self):
        self.assertEqual(self.request('/api/info',auth=False)['_status'],401)
        self.assertEqual(self.request('/api/start',{},headers={'Origin':'https://untrusted.example'})['_status'],403)
        self.assertEqual(self.request('/api/info',headers={'Host':'untrusted.example'})['_status'],403)
        self.assertNotIn('token',self.request('/api/info'))
    def test_text_chat_uses_same_core_and_physicalize_is_fixed_before_start(self):
        self.start(True);result=self.request('/api/send',{'visit':self.visit,'text':'Hello','physicalize':False})
        self.assertEqual(result['reply'],'A fixture reply.')
        self.assertEqual(self.turns[0][1]['mode'],'phone');self.assertTrue(self.turns[0][1]['physicalize'])
        log=next(config.EPISODIC_DIR.glob('chat-phone-*.md')).read_text()
        self.assertIn('Physicalize was enabled',log);self.assertIn('not events that physically happened',log)
        info=self.request('/api/info');self.assertEqual(info['history'][0]['content'],'Hello')
    def test_different_device_and_old_visit_cannot_send(self):
        self.start();other=self.house.pairing.exchange(self.house.pairing.data['code'])
        self.assertEqual(self.request('/api/send',{'visit':'old','text':'Hello'})['_status'],409)
        self.assertEqual(self.request('/api/send',{'visit':self.visit,'text':'Hello'},auth=False,headers={'Authorization':'Bearer '+other})['_status'],409)
        self.assertEqual(self.turns,[])
    def test_voice_routes_keep_native_audio_format_and_spoken_chat_path(self):
        self.start();v={'visit':self.visit}
        self.request('/api/voice/start',v)
        result=self.request('/api/voice/turn',dict(v,audio=audio()))
        self.assertEqual(result['transcript'],'That is Squadron 42.')
        self.assertEqual(self.turns[-1][1]['mode'],'phone_voice')
        self.assertTrue(result['speech_job'])
        trans=next(k for op,k in self.worker.calls if op=='transcribe')
        self.assertEqual(trans['audio'],audio())
        status=self.request('/api/voice/status',dict(v,job=result['speech_job']))
        self.assertEqual(status['chunks'][0]['index'],0)
        req=urllib.request.Request(self.base+'/api/audio/'+result['speech_job']+'/0?visit='+self.visit,
            headers={'Authorization':'Bearer '+self.token})
        with urllib.request.urlopen(req) as response:self.assertTrue(response.read().startswith(b'RIFF'))
        self.request('/api/voice/ack',dict(v,job=result['speech_job'],index=0))
        self.request('/api/voice/delivery',dict(v,job=result['speech_job'],completed=1,partial=False))

    def test_recognition_rejects_noise_without_interrupting_and_accepts_before_reply_generation(self):
        self.start();v={'visit':self.visit};self.request('/api/voice/start',v)
        previous=self.request('/api/send',dict(v,text='Tell me a story.',spoken=True))
        job=previous['speech_job'];session=self.house.session
        epoch=session.playback.epoch;version=session.initiative.version;before=len(self.turns)
        original=self.worker.request
        def noise(op,**kwargs):
            return {'text':''} if op=='transcribe' else original(op,**kwargs)
        with patch.object(self.worker,'request',side_effect=noise):
            rejected=self.request('/api/voice/recognize',dict(v,audio=audio()))
        self.assertTrue(rejected['empty'])
        self.assertFalse(session.playback.status(job)['cancelled'])
        self.assertEqual(session.playback.epoch,epoch);self.assertEqual(session.initiative.version,version)
        self.assertEqual(len(self.turns),before)
        accepted=self.request('/api/voice/recognize',dict(v,audio=audio()))
        self.assertTrue(accepted['accepted']);self.assertEqual(len(self.turns),before)
        self.assertTrue(session.playback.status(job)['cancelled'])
        response=self.request('/api/voice/turn',dict(v,input=accepted['input'],text='Untrusted replacement'))
        self.assertEqual(response['transcript'],'That is Squadron 42.')
        self.assertEqual(self.request('/api/voice/turn',dict(v,input=accepted['input']))['_status'],400)
        self.assertEqual(len(self.turns),before+1)

    def test_recognition_proofs_are_cancelled_by_stop_and_invalid_audio_does_not_interrupt(self):
        self.start();v={'visit':self.visit};self.request('/api/voice/start',v)
        epoch=self.house.session.playback.epoch;version=self.house.session.initiative.version
        result=self.request('/api/voice/recognize',dict(v,audio=base64.b64encode(b'not wav').decode()))
        self.assertEqual(result['_status'],400)
        self.assertEqual(self.house.session.playback.epoch,epoch);self.assertEqual(self.house.session.initiative.version,version)
        accepted=self.request('/api/voice/recognize',dict(v,audio=audio()))
        self.request('/api/voice/interrupt',v)
        self.assertEqual(self.request('/api/voice/turn',dict(v,input=accepted['input']))['_status'],400)
        self.assertEqual(self.turns,[])
    def test_near_ear_context_tracks_applied_position_only_in_physicalized_spoken_turns(self):
        self.start(True)
        self.request('/api/voice/start',{'visit':self.visit})
        self.request('/api/spatial/configure',{'settings':{'enabled':True,'azimuth':90,'distance':.11}})
        self.request('/api/send',{'visit':self.visit,'text':'Hello','spoken':True})
        p=self.turns[-1][1]['physicalize_voice']
        self.assertTrue(p['close']);self.assertEqual(p['ear'],'right');self.assertEqual(p['distance_cm'],2)
        rendered=chat.render_turn(self.house.session.history[0])['content']
        self.assertIn("keeper's right ear",rendered)
        self.assertEqual(self.house.session.history[0]['content'],'Hello')
        self.request('/api/spatial/configure',{'settings':{'enabled':True,'azimuth':90,'distance':.5}})
        self.request('/api/send',{'visit':self.visit,'text':'Further away','spoken':True})
        self.assertFalse(self.turns[-1][1]['physicalize_voice']['close'])
        self.assertIn('Earlier close-to-ear placement cues no longer apply',chat.render_turn(self.house.session.history[-2])['content'])
        self.request('/api/send',{'visit':self.visit,'text':'Typed message'})
        self.assertNotIn('physicalize_voice',self.turns[-1][1])

    def test_position_does_not_add_embodiment_when_physicalize_is_off(self):
        self.start(False)
        self.request('/api/voice/start',{'visit':self.visit})
        self.request('/api/spatial/configure',{'settings':{'enabled':True,'azimuth':-90,'distance':.11}})
        self.request('/api/send',{'visit':self.visit,'text':'Hello','spoken':True})
        self.assertNotIn('physicalize_voice',self.turns[-1][1])

    def test_invalid_microphone_audio_never_reaches_recognizer(self):
        self.start();v={'visit':self.visit};self.request('/api/voice/start',v)
        result=self.request('/api/voice/turn',dict(v,audio=base64.b64encode(b'not wav').decode()))
        self.assertEqual(result['_status'],400)
        self.assertFalse(any(op=='transcribe' for op,_ in self.worker.calls))

    def test_initiative_requires_current_owner_and_passes_phone_presence(self):
        self.start(True);v={'visit':self.visit}
        self.request('/api/voice/start',v)
        self.assertEqual(self.house.session.initiative.source,'phone')
        self.request('/api/spatial/configure',{'settings':{'enabled':True,'azimuth':90,'distance':.11}})
        with patch.object(self.house.session,'voice_tick',return_value={'quiet':True}) as tick:
            self.assertTrue(self.request('/api/voice/initiative',v)['quiet'])
            tick.assert_not_called()
            clock=self.house.session.initiative;clock.heard();clock.speaking('fixture');clock.finished('fixture');clock.due=0
            self.house.tick()
            deadline=time.monotonic()+2
            while self.house.initiative_task is not None and time.monotonic()<deadline:time.sleep(.005)
            self.assertEqual(tick.call_args.kwargs['mode'],'phone_voice')
            self.assertTrue(tick.call_args.kwargs['physicalize'])
            self.assertEqual(tick.call_args.kwargs['physicalize_voice']['ear'],'right')
            other=self.house.pairing.exchange(self.house.pairing.data['code'])
            result=self.request('/api/voice/initiative',v,auth=False,headers={'Authorization':'Bearer '+other})
            self.assertEqual(result['_status'],409)
            self.assertEqual(tick.call_count,1)
            self.request('/api/voice/stop',v)
            self.assertTrue(self.request('/api/voice/initiative',v)['quiet'])
            self.assertEqual(tick.call_count,1)

    def test_phone_diagnostic_reports_are_read_only_and_deadlines_log_without_a_check_request(self):
        from contextlib import redirect_stdout
        import voice_initiative
        self.start();v={'visit':self.visit};self.request('/api/voice/start',v)
        now=[0.0];clock=voice_initiative.Clock(lambda:now[0],lambda *_:9,source='phone')
        self.house.session.initiative=clock;output=io.StringIO()
        with patch.object(config,'LIVE_VOICE_INITIATIVE_LOG',True),redirect_stdout(output):
            clock.start(scheduler='server');clock.heard();clock.speaking('fixture');clock.finished('fixture')
            before=(clock.version,clock.due,self.house.session.playback.epoch,len(self.turns))
            status={'blocked':['capture'],'phase':'listening','mic_age_ms':0}
            self.assertTrue(self.request('/api/voice/initiative/state',dict(v,state=status))['ok'])
            self.house.initiative_task=object() # A scheduler task is already queued; diagnostics must remain observational.
            now[0]=9;self.house.tick();self.assertTrue(voice_initiative.flush_console())
            self.assertIn('Check deadline reached',output.getvalue())
            self.assertIn('waiting for the PC scheduler',output.getvalue())
            self.assertNotIn('unfinished microphone capture',output.getvalue())
            self.assertEqual(before,(clock.version,clock.due,self.house.session.playback.epoch,len(self.turns)))
            other=self.house.pairing.exchange(self.house.pairing.data['code'])
            result=self.request('/api/voice/initiative/state',dict(v,state=status),auth=False,
                                headers={'Authorization':'Bearer '+other})
            self.assertEqual(result['_status'],409)

    def test_server_checks_and_queues_speech_without_any_phone_poll_or_fresh_client_state(self):
        import voice_initiative
        self.start();v={'visit':self.visit};settings=self.request('/api/voice/start',v)
        self.assertTrue(settings['server_initiative'])
        session=self.house.session;now=[0.0];session.initiative.now=lambda:now[0];session.initiative.gaussian=lambda *_:9
        prior=self.request('/api/send',dict(v,text='Keep telling the story.',spoken=True));job=prior['speech_job']
        deadline=time.monotonic()+2
        while not session.playback.status(job)['done'] and time.monotonic()<deadline:time.sleep(.005)
        self.request('/api/voice/delivery',dict(v,job=job,completed=len(session.playback.status(job)['chunks']),partial=False))
        session.initiative.report_client({'blocked':['microphone_stale','playback'],'phase':'speaking','mic_age_ms':120000})
        decision={'text':'The story continues while the screen is locked.','next_check_seconds':9,'wait_for_user':False}
        additions=[{'role':'user','content':'quiet event','_engine':True},{'role':'assistant','content':decision['text']}]
        with patch.object(voice_initiative,'decide',return_value=(decision,additions)) as decide:
            now[0]=8;self.house.tick();decide.assert_not_called()
            now[0]=9;self.house.tick()
            deadline=time.monotonic()+2
            while self.house.initiative_task is not None and time.monotonic()<deadline:time.sleep(.005)
            self.assertEqual(decide.call_count,1)
            self.house.tick();self.assertEqual(decide.call_count,1)
            event=self.request('/api/voice/events',dict(v,after=0))['events'][0]
            self.assertEqual(event['reply'],decision['text']);self.assertTrue(event['native_playback'])
            self.assertEqual(self.request('/api/voice/events',dict(v,after=event['sequence']))['events'],[])
            other=self.house.pairing.exchange(self.house.pairing.data['code'])
            self.assertEqual(self.request('/api/voice/events',dict(v,after=0),auth=False,headers={'Authorization':'Bearer '+other})['_status'],409)
            job=event['speech_job'];deadline=time.monotonic()+2
            while not session.playback.status(job)['done'] and time.monotonic()<deadline:time.sleep(.005)
            self.request('/api/voice/delivery',dict(v,job=job,completed=len(session.playback.status(job)['chunks']),partial=False))
            now[0]=18;self.house.tick()
            deadline=time.monotonic()+2
            while self.house.initiative_task is not None and time.monotonic()<deadline:time.sleep(.005)
            self.assertEqual(decide.call_count,2)

    def test_late_locked_screen_controls_cannot_stop_a_restarted_voice_session(self):
        self.start();v={'visit':self.visit};old=self.request('/api/voice/start',v)['voice_version']
        fresh=self.request('/api/voice/start',dict(v,restart=True))['voice_version'];self.assertNotEqual(old,fresh)
        epoch=self.house.session.playback.epoch
        for route in ['/api/voice/interrupt','/api/voice/stop']:
            self.assertTrue(self.request(route,dict(v,voice_version=old))['stale'])
        self.assertTrue(self.house.voice);self.assertEqual(self.house.session.playback.epoch,epoch)
    def test_desktop_spatial_is_paired_and_produces_stereo_with_a_tail(self):
        self.assertEqual(self.request('/api/spatial/configure',{'settings':{'enabled':True}},auth=False)['_status'],401)
        chosen=self.request('/api/spatial/configure',{'settings':{'enabled':True,'azimuth':90,'distance':.11}})
        self.assertEqual(chosen['nearest_ear_cm'],2)
        self.start();v={'visit':self.visit};self.request('/api/voice/start',v)
        reply=self.request('/api/voice/turn',dict(v,audio=audio()));job=reply['speech_job']
        deadline=time.time()+2
        while time.time()<deadline:
            status=self.request('/api/voice/status',dict(v,job=job))
            if status['done']:break
            time.sleep(.01)
        self.assertEqual(status['spatial']['channels'],2);self.assertEqual(len(status['chunks']),2)
        raw=self.house.session.playback.audio(job,0)
        with wave.open(io.BytesIO(raw)) as f:self.assertEqual(f.getnchannels(),2);self.assertEqual(f.getsampwidth(),3)
        self.assertTrue(any(op=='finish' for op,data in self.spatial.calls))
        self.assertTrue(self.request('/api/spatial/preview',v)['audio'])
    def test_bad_spatial_values_and_other_device_cannot_change_active_position(self):
        self.assertEqual(self.request('/api/spatial/configure',{'settings':{'distance':float('nan')}})['_status'],400)
        self.start();other=self.house.pairing.exchange(self.house.pairing.data['code'])
        self.assertEqual(self.request('/api/spatial/configure',{'settings':{'enabled':True}},auth=False,headers={'Authorization':'Bearer '+other})['_status'],409)
    def test_delivery_controls_preview_and_spoken_job_share_preferences_without_a_memory_turn(self):
        self.assertEqual(self.request('/api/delivery/configure',{'settings':{}},auth=False)['_status'],401)
        chosen={'speed':.85,'whisper_mix':.3,'pause_ms':220,'guidance':2}
        self.assertTrue(self.request('/api/delivery/configure',{'settings':chosen})['ok'])
        preview=self.request('/api/delivery/preview',{'settings':chosen})
        self.assertTrue(preview['audio']);self.assertEqual(self.turns,[])
        self.assertTrue(self.worker.closed) # Pre-visit preview releases the speech models.
        self.assertEqual(self.worker.calls[-1][1]['delivery'],chosen)
        v=self.request('/api/start',{})
        self.assertEqual(v['delivery'],chosen)
        self.request('/api/voice/start',v)
        r=self.request('/api/voice/turn',dict(v,audio=audio()))
        deadline=time.time()+3
        while not self.house.session.playback.status(r['speech_job'])['done'] and time.time()<deadline:time.sleep(.01)
        self.assertEqual(next(k for op,k in self.worker.calls if op=='synthesize_stream')['delivery'],chosen)
        other=self.house.pairing.exchange(self.house.pairing.data['code'])
        self.assertEqual(self.request('/api/delivery/configure',{'settings':chosen},auth=False,headers={'Authorization':'Bearer '+other})['_status'],409)
        self.assertEqual(self.request('/api/delivery/configure',{'settings':{'guidance':1.1}})['_status'],400)

    def test_preview_respects_busy_session_and_already_running_preview(self):
        self.house.previewing=True
        self.assertEqual(self.request('/api/delivery/preview',{})['_status'],409)
        self.assertEqual(self.request('/api/start',{})['_status'],409)
        self.house.previewing=False;self.request('/api/start',{})
        with self.house.session.lock:
            self.assertEqual(self.request('/api/delivery/preview',{})['_status'],409)

    def test_file_upload_is_confined_to_shared_and_model_gets_the_reference(self):
        self.start();req=urllib.request.Request(self.base+'/api/upload',data=b'A small letter.',headers={
            'Authorization':'Bearer '+self.token,'X-Visit':self.visit,'X-Filename':'..%2Fkeeper.md'})
        with urllib.request.urlopen(req) as response:result=json.load(response)
        path=(self.root/result['path']).resolve();self.assertTrue(path.is_relative_to(config.SHARED_DIR))
        self.assertEqual(path.read_bytes(),b'A small letter.');self.assertFalse((self.root/'keeper.md').exists())
        self.request('/api/send',{'visit':self.visit,'text':'Have a look.'})
        self.assertIn(result['path'],self.turns[0][0])
        self.assertEqual(self.request('/api/info')['history'][0]['content'],'Have a look.')
    def test_end_saves_the_visit_and_releases_the_model_lease(self):
        self.start();self.request('/api/send',{'visit':self.visit,'text':'Hello'})
        old=self.visit;self.request('/api/leave',{'visit':old})
        deadline=time.time()+2
        while self.house.closing and time.time()<deadline:time.sleep(.01)
        self.assertFalse(self.house.closing);self.assertTrue(self.worker.closed)
        self.assertFalse(self.house.visit);self.assertIsNone(self.house.lease)
        self.assertTrue(list(config.EPISODIC_DIR.glob('chat-phone-*.md')))
        self.assertEqual(self.request('/api/ping',{'visit':old})['_status'],409)
    def test_phone_marker_remains_live_and_idle_disconnection_finishes(self):
        self.start();self.house.seen=time.monotonic()-phone.IDLE_SECONDS-1;self.house.tick()
        deadline=time.time()+2
        while self.house.closing and time.time()<deadline:time.sleep(.01)
        self.assertFalse(self.house.visit)
    def test_private_code_is_visible_only_on_loopback_setup_page(self):
        with urllib.request.urlopen(self.base+'/') as response:self.assertNotIn(self.house.pairing.data['code'].encode(),response.read())
        with urllib.request.urlopen(self.base+'/setup') as response:self.assertIn(self.house.pairing.data['code'].encode()[:4],response.read())


class PresenceTests(unittest.TestCase):
    def test_real_turn_runner_renders_position_metadata_without_changing_user_words(self):
        history=[]
        p={'enabled':True,'close':True,'ear':'left','distance_cm':2}
        with patch.object(chat.assemble,'system_prompt',return_value='Fixture identity'), \
             patch.object(chat.assemble,'moment',return_value=('',[])), \
             patch.object(chat.ollama_client,'chat',return_value={'role':'assistant','content':'A fixture reply.'}) as run, \
             patch.object(chat.tools,'refresh_her_tools'),patch.object(chat,'mark_visit_live'):
            chat.one_turn(history,'Hello',mode='phone_voice',physicalize=True,physicalize_voice=p,on_event=lambda *a:None)
        self.assertEqual(history[0]['content'],'Hello')
        self.assertEqual(history[0]['_physicalize_voice'],p)
        messages=run.call_args.args[0]
        self.assertTrue(any("keeper's left ear" in m.get('content','') for m in messages))

    def test_ear_distance_includes_height_direction_and_head_size(self):
        from spatial import voice_position
        for angle,ear in [(-90,'left'),(90,'right')]:
            p=voice_position({'enabled':True,'azimuth':angle,'distance':.11})
            self.assertEqual(p['ear'],ear);self.assertTrue(p['close']);self.assertEqual(p['distance_cm'],2)
        self.assertFalse(voice_position({'enabled':True,'azimuth':90,'distance':.19})['close'])
        self.assertTrue(voice_position({'enabled':True,'azimuth':90,'distance':.189})['close'])
        self.assertFalse(voice_position({'enabled':True,'azimuth':0,'distance':.11})['close'])
        self.assertFalse(voice_position({'enabled':True,'azimuth':90,'elevation':60,'distance':.19})['close'])
        self.assertEqual(voice_position({'enabled':False}),{'enabled':False,'close':False})
    def test_presence_changes_rendered_context_without_rewriting_user_words(self):
        t={'role':'user','content':'Hello','_physicalize':True,'_voice_mode':True,'_voice_phone':True}
        rendered=chat.render_turn(t)
        self.assertIn('phone app',rendered['content']);self.assertIn('Treat the phone as your body',rendered['content'])
        self.assertEqual(t['content'],'Hello');self.assertNotIn('Physicalize',chat.render_turn({'role':'user','content':'Hello'})['content'])
    def test_normal_phone_is_a_visit_not_an_autonomous_wake(self):
        with patch.object(assemble,'identity',return_value='Fixture identity'),patch.object(assemble.memory,'count',return_value=0), \
            patch.object(assemble,'journal_tail',return_value='Fixture journal'):
            normal=assemble.system_prompt('',mode='phone',warm=True)
            embodied=assemble.system_prompt('',mode='phone_voice',warm=True,physicalize=True)
        self.assertIn('here with you through the phone app',normal)
        self.assertNotIn('Nobody is talking to you right now',normal)
        self.assertNotIn(assemble.PHYSICALIZE,normal);self.assertIn(assemble.PHYSICALIZE,embodied)


if __name__=='__main__':unittest.main()
