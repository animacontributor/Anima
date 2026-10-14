"""Handy integration contracts: fake HTTP only, no device motion or personal writes."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))
import assemble
import config
import handy
import panel
import tools
import voice_initiative


class Device:
    def __init__(self):
        self.connected = True
        self.mode = 7
        self.velocity = 0.2
        self.running = False
        self.calls = []
        self.fail = {}
        self.auth_headers = []
        self.replies = {}  # acknowledged command/state snapshots, possibly transitional
        self.reset_velocity_on_start = False
        self.patterns = [{"id": i + 100, "slot": i, "name": f"Pattern {i}", "type": 1} for i in range(10)]
        self.pattern = self.patterns[0]
        self.rate, self.amplitude = 1.0, 1.0
        self.pattern_list_wrapped = False
        self.rotate_pattern_ids_on_mode_change = False

    def __call__(self, request, timeout):
        path = request.full_url.removeprefix(handy.API_URL)
        data = json.loads(request.data) if request.data is not None else None
        self.calls.append((request.get_method(), path, data, timeout))
        self.auth_headers.append(dict(request.header_items()))
        if path in self.fail:
            failure = self.fail[path]
            if isinstance(failure, Exception):
                raise failure
            return io.BytesIO(json.dumps(failure).encode())
        if path.startswith("/auth/token/issue?"):
            result = {"token": "issuedTokenSecret", "expires_at":
                      (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
        elif path == "/connected":
            result = {"connected": self.connected}
        elif path == "/mode":
            result = {"mode": self.mode}
        elif path == "/mode2":
            self.mode = data["mode"]
            self.running = False
            if self.rotate_pattern_ids_on_mode_change and self.mode==9:
                for pattern in self.patterns:pattern['id']+=1000
            result = {"mode": self.mode, "mode_session_id": 42}
        elif path == "/hrpp/patterns":
            result = {"patterns": self.patterns} if self.pattern_list_wrapped else self.patterns
        elif path.startswith("/hrpp/"):
            if path.endswith('/activate'):
                self.pattern = next(p for p in self.patterns if p['slot'] == int(path.split('/')[3]))
            elif path == '/hrpp/start': self.running = True
            elif path == '/hrpp/stop': self.running = False
            elif path == '/hrpp/playbackrate': self.rate = data['playback_rate']
            elif path == '/hrpp/amplitude': self.amplitude = data['amplitude']
            result = {'play_state': 2 if self.running else 1, 'current_pattern': self.pattern,
                      'playback_rate': self.rate, 'amplitude': self.amplitude}
        elif path.startswith("/hamp/"):
            if path == "/hamp/velocity":
                self.velocity = data["velocity"]
            elif path == "/hamp/start":
                self.running = True
                if self.reset_velocity_on_start:
                    self.velocity = 0
            result = {"play_state": int(self.running), "velocity": self.velocity, "direction": True}
        else:
            raise AssertionError(path)
        if self.replies.get(path):
            result = self.replies[path].pop(0)
        return io.BytesIO(json.dumps({"result": result}).encode())


class HandyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.saved = Path(self.temp.name) / "handy.json"
        self.saved.write_text(json.dumps({"connection_key": "deviceSecret", "api_token": "tokenSecret"}))
        self.device = Device()
        self.console = io.StringIO()
        capture = redirect_stdout(self.console)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)
        patches = [patch.object(config, "MEMORY_DIR", Path(self.temp.name)),
                   patch.object(config, "HANDY_SPEED_COMMANDS", True),
                   patch.object(config, "HANDY_MIN_SPEED", 10),
                   patch.object(config, "HANDY_MAX_SPEED", 50),
                   patch.object(config, "HANDY_TIMEOUT_S", 3),
                   patch.object(config, "HANDY_STATUS_CACHE_S", 10),
                   patch.object(config, "HANDY_PATTERN_MIN_RATE", .1),
                   patch.object(config, "HANDY_PATTERN_MAX_RATE", 1.0),
                   patch.object(config, "HANDY_PATTERN_MAX_AMPLITUDE", 100),
                   patch.object(config, "TOOL_KIT", "full"),
                   patch.object(config, "OFFLINE", False),
                   patch.dict(os.environ, {"HANDY_CONNECTION_KEY": "", "HANDY_API_TOKEN": "", "HANDY_APPLICATION_KEY": ""}),
                   patch.object(handy, "_issued_token", None),
                   patch.object(handy.urllib.request, "urlopen", side_effect=self.device)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        handy.invalidate()
        self.addCleanup(handy.invalidate)

    def names(self):
        return {d["function"]["name"] for d in tools.current_definitions()}

    def use_application_key(self):
        self.saved.write_text(json.dumps({"connection_key": "deviceSecret", "application_key": "applicationSecret",
                                         "api_token": "oldLoginSecret"}))

    def test_all_patterns_are_listed_read_only_in_both_documented_shapes(self):
        for wrapped in (False, True):
            self.device.pattern_list_wrapped = wrapped
            result = json.loads(tools.list_device_patterns())
            self.assertEqual(len(result['patterns']), 10)
            self.assertEqual(result['patterns'][0]['selector'], 'slot:0')
        self.assertTrue(all(c[0] == 'GET' for c in self.device.calls))
        self.assertEqual(self.device.mode, 7)

    def test_keeper_labels_descriptions_and_exclusions_survive_slot_order_changes(self):
        self.assertTrue(panel.handy_patterns()['ok'])
        self.assertTrue(panel.handy_patterns([
            {'id':100,'name':'Gentle waves','description':'Slow changes in rhythm','enabled':True},
            {'id':101,'name':'Unwanted','enabled':False}])['ok'])
        self.device.patterns[0]['slot'] = 20
        result = handy.list_device_patterns()['patterns']
        self.assertEqual(len(result), 9)
        self.assertEqual(result[0]['name'], 'Gentle waves')
        self.assertEqual(result[0]['description'], 'Slow changes in rhythm')
        self.assertEqual(result[0]['selector'], 'slot:20')
        self.assertIn('Gentle waves', assemble.handy_section())
        self.assertNotIn('Unwanted', assemble.handy_section())
        self.assertEqual(len(panel.handy_patterns()['patterns']), 10)
        self.assertTrue(all(c[0] == 'GET' for c in self.device.calls))
        self.assertNotIn('Secret', (self.saved.parent/'handy-patterns.json').read_text())

    def test_aliases_and_exclusions_survive_firmware_id_changes(self):
        rows = handy.list_device_patterns(include_disabled=True)['patterns']
        changes = [dict(p, name='Gentle waves' if p['slot']==0 else p['name'], enabled=p['slot']!=1) for p in rows]
        saved = handy.save_pattern_names(changes)
        self.assertEqual(saved['path'], str((self.saved.parent/'handy-patterns.json').resolve()))
        for p in self.device.patterns: p['id'] += 10000
        handy.invalidate()
        result = handy.list_device_patterns(include_disabled=True)['patterns']
        self.assertEqual(result[0]['name'], 'Gentle waves')
        self.assertFalse(result[1]['enabled'])
        self.assertEqual(result[0]['preference_key'], 'name:pattern 0')
        handy.save_pattern_names([dict(p, enabled=p['slot'] not in (1,2)) for p in result])
        settings = json.loads((self.saved.parent/'handy-patterns.json').read_text())['patterns']
        self.assertEqual(len(settings),10)
        self.assertTrue(all(k.startswith('name:') for k in settings))
        self.assertFalse(settings['name:pattern 2']['enabled'])
        self.assertTrue(all(c[0]=='GET' for c in self.device.calls))

    def test_legacy_factory_name_preferences_migrate_without_duplicate_name_rejection(self):
        legacy = {str(p['id']): {'name':p['name'],'description':'','enabled':p['slot']!=1} for p in self.device.patterns}
        path=self.saved.parent/'handy-patterns.json';path.write_text(json.dumps({'patterns':legacy}))
        for p in self.device.patterns: p['id'] += 10000
        rows = handy.list_device_patterns(include_disabled=True)['patterns']
        self.assertFalse(rows[1]['enabled'])
        result = panel.handy_patterns([dict(p, enabled=p['slot'] not in (1,2)) for p in rows])
        self.assertTrue(result['ok'], result)
        settings = json.loads(path.read_text())['patterns']
        self.assertEqual(len(settings),10)
        self.assertFalse(settings['name:pattern 1']['enabled'])
        self.assertFalse(settings['name:pattern 2']['enabled'])
        self.assertFalse(any(k.isdecimal() for k in settings))
        self.assertTrue(all(c[0]=='GET' for c in self.device.calls))

    def test_named_preferences_save_locally_after_device_disconnects(self):
        rows = handy.list_device_patterns(include_disabled=True)['patterns']
        self.device.connected=False;self.device.calls.clear()
        self.assertTrue(panel.handy_patterns([dict(p, enabled=p['slot']!=1) for p in rows])['ok'])
        self.assertFalse(self.device.calls)
        self.assertFalse(json.loads((self.saved.parent/'handy-patterns.json').read_text())['patterns']['name:pattern 1']['enabled'])

    def test_duplicate_invalid_names_are_refused_without_overwriting_choices(self):
        for rows in ([{'id':100,'name':'Same'},{'id':101,'name':'same'}],
                     [{'id':100,'name':'slot:0'}], [{'id':100,'name':'line\nbreak'}],
                     [{'id':True,'name':'A'}], [{'id':100,'name':'x'*121}]):
            self.assertFalse(panel.handy_patterns(rows)['ok'])
            self.assertFalse((self.saved.parent/'handy-patterns.json').exists())
        self.assertFalse(self.device.calls)

    def test_unknown_and_excluded_patterns_never_send_motion(self):
        handy.save_pattern_names([{'id':100,'name':'Disabled','enabled':False}])
        for pattern in ('slot:0','Disabled','not a pattern'):
            self.assertIn('unknown, disabled or ambiguous', tools.play_device_pattern(pattern))
        self.assertTrue(all(c[0] == 'GET' for c in self.device.calls))

    def test_keeper_can_test_excluded_patterns_while_friend_still_cannot_select_them(self):
        handy.save_pattern_names([{'id':101,'name':'Not for the friend','enabled':False}])
        self.assertNotIn('Not for the friend', handy.section())
        result=panel.handy_pattern_test(1,.5,70)
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['status']['pattern'],'Not for the friend')
        self.assertTrue(self.device.running)
        self.assertEqual((self.device.rate,self.device.amplitude),(.5,.7))
        self.assertTrue(panel.handy_stop()['ok'])
        self.assertFalse(self.device.running)
        self.assertIn('unknown, disabled or ambiguous', tools.play_device_pattern('slot:1'))
        self.assertFalse(any('slider' in c[1] or 'stroke' in c[1] for c in self.device.calls))

    def test_panel_tests_validate_inputs_and_use_configured_limits(self):
        for args in ((True,.5,100),(-1,.5,100),(1,None,100),(1,2,100),(1,.5,101),(1,.5,True)):
            self.assertFalse(panel.handy_pattern_test(*args)['ok'])
        self.assertFalse(self.device.calls)
        with patch.object(config,'HANDY_PATTERN_MAX_RATE',.4),patch.object(config,'HANDY_PATTERN_MAX_AMPLITUDE',60):
            result=panel.handy_patterns()
            self.assertEqual(result['test_limits']['rate'],1.0)
            self.assertEqual(result['test_limits']['max_rate'],1.0)
            self.assertEqual(result['test_limits']['amplitude'],60)
            self.assertTrue(panel.handy_pattern_test(1,.4,60)['ok'])
        panel.handy_stop()

    def test_panel_test_error_attempts_stop_and_is_reported_without_credentials(self):
        self.device.fail['/hrpp/start']=TimeoutError('deviceSecret tokenSecret')
        result=panel.handy_pattern_test(1,.5,100)
        self.assertFalse(result['ok'])
        self.assertIn('device was stopped',result['note'])
        self.assertNotIn('Secret',json.dumps(result))
        self.assertEqual(self.device.mode,7)

    def test_pattern_playback_uses_slots_and_never_writes_slider_limits(self):
        handy.save_pattern_names([{'id':103,'name':'Gentle waves'}])
        result = json.loads(tools._dispatch('play_device_pattern', {'pattern':'Gentle waves'}))
        self.assertTrue(result['running'])
        self.assertEqual(result['pattern_id'], 103)
        self.assertEqual(result['amplitude'], 100)
        writes = [c[1] for c in self.device.calls if c[0] == 'PUT']
        self.assertEqual(writes, ['/mode2','/mode2','/hrpp/patterns/3/activate','/hrpp/playbackrate','/hrpp/amplitude','/hrpp/start'])
        self.assertFalse(any('slider' in c[1] or 'stroke' in c[1] for c in self.device.calls))
        self.assertEqual(handy.get_device_status()['playback_rate'], 1.0)
        self.assertIn('pattern mode; playing', assemble.handy_section())
        self.assertIsNone(handy.get_device_status()['speed'])
        self.assertTrue(handy.stop_device()['stopped'])

    def test_pattern_change_stops_old_motion_before_activation(self):
        self.device.mode, self.device.running = 9, True
        handy.play_device_pattern('slot:5', .5)
        self.assertEqual([(c[1],c[2]) for c in self.device.calls if c[0] == 'PUT'][:2], [('/mode2',{'mode':7}),('/mode2',{'mode':9})])
        self.assertEqual((self.device.rate,self.device.amplitude), (.5,1.0))

    def test_friend_and_panel_switch_patterns_with_stop_first_when_firmware_ids_change(self):
        self.device.rotate_pattern_ids_on_mode_change=True
        rows=handy.list_device_patterns(include_disabled=True)['patterns']
        handy.save_pattern_names([dict(p,max_rate=.5) for p in rows])
        for slot in (0,3,0,4):
            self.device.calls.clear()
            result = (handy.play_device_pattern(f'slot:{slot}',.5) if slot%2==0
                      else panel.handy_pattern_test(slot,.5,100)['status'])
            self.assertTrue(result['running'])
            self.assertTrue(self.device.running)
            self.assertEqual(result['pattern_slot'],slot)
            self.assertEqual(result['pattern_device_name'],f'Pattern {slot}')
            self.assertEqual(result['device_playback_rate'],.25)
            writes=[(c[1],c[2]) for c in self.device.calls if c[0]=='PUT']
            self.assertEqual(writes[:2],[('/mode2',{'mode':7}),('/mode2',{'mode':9})])
            self.assertEqual(writes[-1],('/hrpp/start',{}))
            self.assertEqual(sum(path=='/mode2' and data=={'mode':7} for path,data in writes),1)
            self.assertFalse(any('stroke' in c[1] or 'slider' in c[1] for c in self.device.calls))

    def test_pattern_verification_rejects_wrong_factory_name_even_in_requested_slot(self):
        self.device.replies['/hrpp/state']=[
            {'play_state':1,'current_pattern':self.device.patterns[0],'playback_rate':1,'amplitude':1},
            {'play_state':2,'current_pattern':dict(self.device.patterns[0],name='Different preset'),'playback_rate':1,'amplitude':1}]
        self.assertIn('not confirmed',tools.play_device_pattern('slot:0'))
        self.assertEqual(self.device.mode,7)
        self.assertFalse(self.device.running)

    def test_pattern_rate_and_amplitude_changes_do_not_start_or_restart_playback(self):
        self.device.mode, self.device.running = 9, True
        self.assertEqual(handy.set_pattern_playback_rate(.5)['playback_rate'], .5)
        self.assertEqual(handy.set_pattern_amplitude(40)['amplitude'], 40)
        self.assertEqual([c[1] for c in self.device.calls if c[0] == 'PUT'], ['/hrpp/playbackrate','/hrpp/amplitude'])
        self.device.calls.clear();self.device.mode = 0
        self.assertIn('select a device pattern first', tools.set_pattern_playback_rate(.5))
        self.assertTrue(all(c[0] == 'GET' for c in self.device.calls))

    def test_pattern_limits_and_invalid_types_refuse_before_any_request(self):
        for rate in (None,True,'0.5',0,-1,1.1,float('nan'),float('inf')):
            self.assertTrue(tools.set_pattern_playback_rate(rate).startswith('('))
        for amplitude in (None,True,'50',1.5,-1,101):
            with self.assertRaises(handy.HandyError):
                handy.set_pattern_amplitude(amplitude)
        for kwargs in ({'playback_rate':True},{'amplitude':True},{'playback_rate':2},{'amplitude':101}):
            self.assertTrue(tools._dispatch('play_device_pattern',{'pattern':'slot:1',**kwargs}).startswith('('))
        with patch.object(config,'HANDY_PATTERN_MAX_AMPLITUDE',0):
            self.assertIn('disabled', tools.play_device_pattern('slot:1'))
        self.assertFalse(self.device.calls)

    def test_custom_maximum_scales_play_and_rate_changes_without_double_scaling(self):
        rows=handy.list_device_patterns(include_disabled=True)['patterns']
        self.assertTrue(panel.handy_patterns([dict(p,max_rate=.5 if p['slot']==0 else .8) for p in rows])['ok'])
        result=json.loads(tools.play_device_pattern('slot:0',.5))
        self.assertEqual(result['playback_rate'],.5)
        self.assertEqual(result['device_playback_rate'],.25)
        self.assertEqual((self.device.rate,self.device.amplitude),(.25,1.0))
        self.assertEqual(handy.get_device_status()['playback_rate'],.5)
        self.assertEqual(handy.get_device_status()['device_playback_rate'],.25)
        self.device.calls.clear()
        result=json.loads(tools.set_pattern_playback_rate(.8))
        self.assertAlmostEqual(result['device_playback_rate'],.4)
        self.assertEqual([c[1] for c in self.device.calls if c[0]=='PUT'],['/hrpp/playbackrate'])
        result=handy.play_device_pattern('slot:0')
        self.assertEqual(result['device_playback_rate'],.5)
        self.assertEqual(result['playback_rate'],1)
        result=handy.play_device_pattern('slot:1',.5)
        self.assertAlmostEqual(result['device_playback_rate'],.4)
        self.assertFalse(any('slider' in c[1] or 'stroke' in c[1] for c in self.device.calls))

    def test_custom_maximum_survives_identity_changes_and_old_label_saves(self):
        rows=handy.list_device_patterns(include_disabled=True)['patterns']
        handy.save_pattern_names([dict(p,max_rate=.5) for p in rows])
        for p in self.device.patterns:p['id']+=1000
        handy.invalidate()
        rows=handy.list_device_patterns(include_disabled=True)['patterns']
        self.assertEqual(rows[0]['max_rate'],.5)
        self.assertEqual(rows[0]['min_playback_rate'],.2)
        self.assertIn('maximum 0.5',handy.section())
        self.assertIn('fraction',handy.section())
        self.assertTrue(panel.handy_pattern_test(0,.5,65)['ok'])
        self.assertEqual((self.device.rate,self.device.amplitude),(.25,.65))
        handy.save_pattern_names([{'id':rows[0]['id'],'name':'Gentle waves','enabled':True}])
        self.assertEqual(handy.list_device_patterns()['patterns'][0]['max_rate'],.5)

    def test_custom_maximum_respects_global_ceiling_and_physical_minimum(self):
        rows=handy.list_device_patterns(include_disabled=True)['patterns']
        handy.save_pattern_names([dict(p,max_rate=.5) for p in rows])
        self.device.calls.clear()
        self.assertIn('>= 0.2',tools.play_device_pattern('slot:0',.1))
        self.assertFalse(any(c[0]=='PUT' for c in self.device.calls))
        with patch.object(config,'HANDY_PATTERN_MAX_RATE',.4):
            result=handy.play_device_pattern('slot:0',.5)
            self.assertEqual(result['effective_max_rate'],.4)
            self.assertEqual(result['device_playback_rate'],.2)
            self.assertEqual(result['playback_rate'],.5)
            self.assertEqual(handy.play_device_pattern('slot:0',.25)['device_playback_rate'],.1)
        self.device.calls.clear()
        for value in (0,-1,True,None,'0.5',float('nan'),float('inf')):
            old=(self.saved.parent/'handy-patterns.json').read_bytes()
            self.assertFalse(panel.handy_patterns([dict(rows[0],max_rate=value)])['ok'])
            self.assertEqual((self.saved.parent/'handy-patterns.json').read_bytes(),old)
        self.assertFalse(self.device.calls)

    def test_maximum_editor_remains_available_with_invalid_global_rate_settings(self):
        with patch.object(config,'HANDY_PATTERN_MAX_RATE',0):
            result=panel.handy_patterns()
            self.assertTrue(result['ok'],result)
            self.assertIsNone(result['test_limits'])
            self.assertIsNone(result['patterns'][0]['effective_max_rate'])
            self.assertTrue(panel.handy_patterns([dict(result['patterns'][0],max_rate=.5)])['ok'])
            self.assertTrue(all(c[0]=='GET' for c in self.device.calls))

    def test_pattern_failure_attempts_stop_without_retrying_motion_or_leaking_secrets(self):
        self.device.fail['/hrpp/start'] = TimeoutError('deviceSecret tokenSecret')
        result = tools.play_device_pattern('slot:1')
        self.assertIn('device was stopped',result)
        self.assertEqual(sum(c[1]=='/hrpp/start' for c in self.device.calls),1)
        self.assertEqual(self.device.mode,7)
        self.assertNotIn('Secret',result)
        self.assertNotIn('Secret', self.console.getvalue())
        self.assertIn('/hrpp/patterns/1/activate', self.console.getvalue())

    def test_legacy_hrpp_state_field_and_wrong_selection_readback(self):
        self.device.mode = 9
        state = {'play_state':2,'current_pattern':self.device.patterns[0],'playback_speed':.5,'amplitude':.75}
        self.device.replies['/hrpp/state'] = [state]
        self.assertEqual(handy.get_device_status()['playback_rate'],.5)
        self.device.replies['/hrpp/state'] = [state,dict(state,play_state=1),dict(state,playback_speed=1,amplitude=1)]
        self.assertIn('not confirmed',tools.play_device_pattern('slot:2'))
        self.assertEqual(self.device.mode,7)

    def test_pattern_setup_avoids_broken_hrpp_stop_in_stopped_and_running_modes(self):
        self.device.fail['/hrpp/stop']=urllib.error.HTTPError(handy.API_URL,500,'remoteSecret',{},None)
        for mode,running in ((7,False),(9,False),(9,True),(0,True)):
            self.device.mode,self.device.running=mode,running
            self.device.calls.clear();handy.invalidate()
            self.assertTrue(handy.play_device_pattern('slot:0',.25)['running'])
            self.assertFalse(any(c[1]=='/hrpp/stop' for c in self.device.calls))
            self.assertEqual((self.device.rate,self.device.amplitude),(.25,1.0))
            if running:
                self.assertEqual(self.device.calls[next(i for i,c in enumerate(self.device.calls) if c[0]=='PUT')][2],{'mode':7})
            handy.stop_device()

    def test_catalog_failure_keeps_basic_controls_available_and_does_not_repeat_every_turn(self):
        self.device.fail['/hrpp/patterns'] = {'error':{'message':'tokenSecret'}}
        self.assertIn('Pattern catalog',assemble.handy_section())
        self.assertIn('set_speed',assemble.handy_section())
        self.assertEqual(sum(c[1]=='/hrpp/patterns' for c in self.device.calls),1)
        self.assertTrue(handy.BASE_TOOL_NAMES <= self.names())

    def test_partial_tool_kit_context_only_mentions_enabled_tools(self):
        with patch.object(config,'TOOL_KIT',['stop_device','get_device_status','list_device_patterns','play_device_pattern']):
            section = assemble.handy_section()
            self.assertIn('Pattern 9', section)
            self.assertIn('play_device_pattern(', section)
            self.assertNotIn('set_pattern_amplitude(', section)
            self.assertNotIn('set_speed(', section)

    def test_malformed_pattern_settings_or_duplicate_device_ids_refuse_without_motion(self):
        path=self.saved.parent/'handy-patterns.json';path.write_text('invalid json')
        self.assertFalse(panel.handy_patterns()['ok'])
        self.assertTrue(tools.play_device_pattern('slot:1').startswith('('))
        path.unlink();self.device.patterns[1]['id']=100
        self.assertTrue(tools.list_device_patterns().startswith('('))
        self.assertTrue(all(c[0] == 'GET' for c in self.device.calls))

    def test_application_key_is_exchanged_and_never_sent_to_device_endpoints(self):
        self.use_application_key()
        status = handy.get_device_status()
        self.assertTrue(status["available"])
        method, path, data, _ = self.device.calls[0]
        self.assertEqual(method, "GET")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        self.assertEqual(query, {"to": ["deviceSecret"], "ttl": ["3600"]})
        self.assertEqual(self.device.auth_headers[0]["X-api-key"], "applicationSecret")
        self.assertEqual(self.device.auth_headers[1]["Authorization"], "Bearer issuedTokenSecret")
        self.assertNotIn("X-api-key", self.device.auth_headers[1])
        self.assertNotIn("Secret", json.dumps(status))
        self.assertNotIn("issuedTokenSecret", self.saved.read_text())

    def test_issued_token_is_reused_then_replaced_before_expiry(self):
        self.use_application_key()
        with patch.object(handy.time, "monotonic", return_value=100):
            handy.get_device_status()
        with patch.object(handy.time, "monotonic", return_value=200):
            handy.get_device_status()
        issued = lambda: sum(c[1].startswith("/auth/token/issue?") for c in self.device.calls)
        self.assertEqual(issued(), 1)
        with patch.object(handy.time, "monotonic", return_value=3700):
            handy.get_device_status()
        self.assertEqual(issued(), 2)

    def test_application_key_change_requires_a_new_token(self):
        self.use_application_key()
        handy.get_device_status()
        with patch.dict(os.environ, {"HANDY_APPLICATION_KEY": "anotherApplicationSecret"}):
            handy.get_device_status()
        auth = [h for h in self.device.auth_headers if "X-api-key" in h]
        self.assertEqual([h["X-api-key"] for h in auth], ["applicationSecret", "anotherApplicationSecret"])

    def test_rejected_application_key_hides_controls_and_is_not_echoed(self):
        self.use_application_key()
        path = "/auth/token/issue?to=deviceSecret&ttl=3600"
        self.device.fail[path] = urllib.error.HTTPError(handy.API_URL, 401, "applicationSecret", {}, None)
        status = handy.get_device_status()
        self.assertFalse(status["available"])
        self.assertNotIn("applicationSecret", json.dumps(status))
        self.assertEqual(len(self.device.calls), 1)
        self.assertNotIn("Application", assemble.handy_section())

    def test_expired_or_malformed_issued_token_is_rejected(self):
        self.use_application_key()
        path = "/auth/token/issue?to=deviceSecret&ttl=3600"
        for result in ({"token": "secret", "expires_at": "bad"},
                       {"token": "secret", "expires_at": "2020-01-01T00:00:00Z"},
                       {"token": "secret", "expires_at": "2030-01-01T00:00:00"},
                       {"expires_at": "2030-01-01T00:00:00Z"}):
            self.device.fail[path] = {"result": result}
            self.assertFalse(handy.get_device_status()["available"])
        self.assertTrue(all(c[1] == path for c in self.device.calls))

    def test_panel_application_key_preserves_other_credentials(self):
        result = panel.secret("handy_application_key", "applicationSecret")
        self.assertTrue(result["ok"])
        saved = json.loads(self.saved.read_text())
        self.assertEqual(saved["connection_key"], "deviceSecret")
        self.assertEqual(saved["api_token"], "tokenSecret")
        self.assertEqual(saved["application_key"], "applicationSecret")
        self.assertNotIn("applicationSecret", json.dumps(result))
        self.assertTrue(panel.secrets_set()["handy_application_key"])

    def test_panel_connection_check_does_not_send_movement(self):
        self.use_application_key()
        result = panel.handy_status()
        self.assertTrue(result["ok"])
        self.assertNotIn("Secret", json.dumps(result))
        self.assertTrue(all(c[0] == "GET" for c in self.device.calls))
        self.device.connected = False
        self.assertIn("offline", panel.handy_status()["note"])

    def test_unconfigured_and_offline_are_silent_without_network(self):
        self.saved.unlink()
        self.assertFalse(handy.available())
        self.assertEqual(assemble.handy_section(), "")
        self.assertTrue(handy.TOOL_NAMES.isdisjoint(self.names()))
        self.assertFalse(self.device.calls)
        self.saved.write_text(json.dumps({"connection_key": "deviceSecret", "api_token": "tokenSecret"}))
        with patch.object(config, "OFFLINE", True):
            self.assertEqual(assemble.handy_section(), "")
            self.assertTrue(handy.TOOL_NAMES.isdisjoint(self.names()))
            with self.assertRaises(handy.HandyError):
                handy.stop_device()
        self.assertFalse(self.device.calls)

    def test_connect_disconnect_reconnect_in_same_session(self):
        self.device.connected = False
        self.assertEqual(assemble.handy_section(), "")
        self.assertTrue(handy.TOOL_NAMES.isdisjoint(self.names()))
        self.device.connected = True
        handy.invalidate()
        section = assemble.handy_section()
        self.assertIn("connected and available", section)
        self.assertIn("10–50%", section)
        self.assertTrue(handy.TOOL_NAMES <= self.names())
        self.device.connected = False
        handy.get_device_status()
        self.assertEqual(assemble.handy_section(), "")
        self.assertTrue(handy.TOOL_NAMES.isdisjoint(self.names()))

    def test_cache_expires_and_live_status_bypasses_it(self):
        with patch.object(handy.time, "monotonic", return_value=100):
            self.assertTrue(handy.available())
        self.device.connected = False
        with patch.object(handy.time, "monotonic", return_value=105):
            self.assertTrue(handy.available())
        with patch.object(handy.time, "monotonic", return_value=111):
            self.assertFalse(handy.available())
        self.device.connected = True
        self.assertTrue(handy.get_device_status()["available"])

    def test_status_is_read_only_and_reports_actual_speed(self):
        self.device.mode = 0
        self.device.running = True
        self.device.velocity = .35
        status = json.loads(tools._dispatch("get_device_status", {}))
        self.assertTrue(status["running"])
        self.assertEqual(status["speed"], 35)
        self.assertEqual((status["min_speed"], status["max_speed"]), (10, 50))
        self.assertTrue(all(c[0] == "GET" for c in self.device.calls))
        self.device.mode = 1
        status = handy.get_device_status()
        self.assertIsNone(status["speed"])
        self.assertIsNone(status["running"])

    def test_maintenance_and_update_modes_hide_controls(self):
        for mode in (3, 5):
            self.device.mode = mode
            handy.invalidate()
            self.assertFalse(handy.available())
            self.assertEqual(assemble.handy_section(), "")
            self.assertTrue(handy.TOOL_NAMES.isdisjoint(self.names()))

    def test_limits_and_types_refuse_without_any_request(self):
        for speed in (-1, 101, 9, 51, 1.0, "20", True, None):
            result = tools._dispatch("set_speed", {"speed": speed})
            self.assertTrue(result.startswith("("), (speed, result))
        self.assertFalse(self.device.calls)

    def test_patterns_only_setting_hides_speed_in_every_kit_and_in_context(self):
        with patch.object(config, 'HANDY_SPEED_COMMANDS', False):
            for kit in ('full', 'small', 'tiny', list(handy.TOOL_NAMES)):
                with patch.object(config, 'TOOL_KIT', kit):
                    self.assertNotIn('set_speed', self.names())
                    self.assertTrue(handy.TOOL_NAMES - {'set_speed'} <= self.names())
                    voice_names = {d['function']['name'] for d in voice_initiative.voice_tools()}
                    self.assertNotIn('set_speed', voice_names)
                    section = assemble.handy_section()
                    self.assertNotIn('set_speed(', section)
                    self.assertIn('play_device_pattern(', section)
                    self.assertIn('normal speed commands are disabled', section)
            self.assertNotIn('set_speed(', handy.section())
        self.assertIn('set_speed', self.names())
        self.assertIn('set_speed(', assemble.handy_section())

    def test_disabled_speed_refuses_stale_calls_but_patterns_and_stop_still_work(self):
        with patch.object(config, 'HANDY_SPEED_COMMANDS', False):
            self.assertIn('disabled', tools._dispatch('set_speed', {'speed': 30}))
            self.assertFalse(self.device.calls)
            self.assertTrue(handy.play_device_pattern('slot:0', .25)['running'])
            self.assertTrue(handy.get_device_status()['available'])
            self.assertTrue(handy.set_speed(0)['stopped'])
            self.assertTrue(panel.handy_pattern_test(0, .25, 30)['ok'])
            self.assertTrue(panel.handy_stop()['ok'])

    def test_friend_has_no_amplitude_tool_or_argument_and_stale_calls_cannot_write(self):
        definitions = {d['function']['name']: d['function'] for d in tools.current_definitions()}
        self.assertNotIn('set_pattern_amplitude', definitions)
        self.assertNotIn('set_pattern_amplitude', tools._BUILTIN_IMPL)
        self.assertNotIn('amplitude', definitions['play_device_pattern']['parameters']['properties'])
        self.assertNotIn('set_pattern_amplitude', handy.section())
        self.assertIn('Amplitude is fixed at 100%', handy.section())
        self.device.calls.clear()
        self.assertTrue(tools._dispatch('play_device_pattern', {'pattern':'slot:0','amplitude':30}).startswith('('))
        self.assertIn('fixed at 100%', tools._dispatch('set_pattern_amplitude', {'amplitude':30}))
        self.assertFalse(any(c[0]=='PUT' for c in self.device.calls))

    def test_friend_play_and_rate_changes_restore_full_amplitude_after_manual_testing(self):
        self.assertTrue(panel.handy_pattern_test(0,.5,30)['ok'])
        self.assertEqual(self.device.amplitude,.3)
        result=json.loads(tools.set_pattern_playback_rate(.25))
        self.assertEqual(result['amplitude'],100)
        self.assertEqual(result['playback_rate'],.25)
        self.assertTrue(self.device.running)
        self.assertTrue(panel.handy_pattern_test(0,.5,30)['ok'])
        result=json.loads(tools.play_device_pattern('slot:0',.25))
        self.assertEqual(result['amplitude'],100)
        self.assertEqual(self.device.amplitude,1.0)
        self.assertFalse(any('slider' in c[1] or 'stroke' in c[1] for c in self.device.calls))

    def test_fixed_amplitude_does_not_override_a_lower_keeper_cap(self):
        with patch.object(config,'HANDY_PATTERN_MAX_AMPLITUDE',60):
            self.assertTrue(tools.play_device_pattern('slot:0').startswith('('))
            self.assertTrue(tools.set_pattern_playback_rate(.25).startswith('('))
            self.assertFalse(self.device.calls)
            self.assertTrue(panel.handy_pattern_test(0,.25,60)['ok'])

    def test_speed_toggle_is_a_boolean_in_senses_and_saves_without_changing_patterns(self):
        path = self.saved.parent / 'engine' / 'config.py'
        path.parent.mkdir()
        path.write_text('HANDY_SPEED_COMMANDS = True\nHANDY_MAX_SPEED = 50\nHANDY_PATTERN_MAX_RATE = 1.0\n')
        with patch.object(panel, 'CONFIG_FILE', path):
            row = next(r for r in panel.tabs()['Senses'] if r['name'] == 'HANDY_SPEED_COMMANDS')
            self.assertEqual(row['kind'], 'bool')
            self.assertTrue(row['value'])
            result = panel.save({'HANDY_SPEED_COMMANDS': False})
            self.assertEqual(result['changed'], ['HANDY_SPEED_COMMANDS'])
            self.assertEqual(result['error'], '')
            self.assertIn('HANDY_SPEED_COMMANDS = False', path.read_text())
            self.assertIn('HANDY_MAX_SPEED = 50', path.read_text())
            self.assertIn('HANDY_PATTERN_MAX_RATE = 1.0', path.read_text())

    def test_invalid_limits_disable_motion_but_allow_stop(self):
        for low, high in ((60, 50), (-1, 50), (1, 101), (True, 50), (1, 2.0)):
            with patch.object(config, "HANDY_MIN_SPEED", low), patch.object(config, "HANDY_MAX_SPEED", high):
                with self.assertRaises(handy.HandyError):
                    handy.set_speed(20)
                self.assertEqual(handy.stop_device(), {"stopped": True, "speed": 0})
        self.assertTrue(all(c[1] == "/mode2" for c in self.device.calls))

    def test_speed_percentage_mapping_and_mode_before_start(self):
        self.assertEqual(json.loads(tools._dispatch("set_speed", {"speed": 30})), {"running": True, "speed": 30})
        self.assertEqual([(c[1], c[2]) for c in self.device.calls if c[0] == "PUT"],
                         [("/mode2", {"mode": 0}), ("/hamp/velocity", {"velocity": .3}),
                          ("/hamp/start", {}), ("/hamp/velocity", {"velocity": .3})])
        self.assertTrue(self.device.running)
        self.assertEqual(handy.get_device_status()["speed"], 30)

    def test_zero_and_stop_bypass_minimum_and_stop_other_protocols(self):
        self.device.mode = 4
        self.device.running = True
        self.assertEqual(handy.set_speed(0), {"stopped": True, "speed": 0})
        self.assertEqual(self.device.mode, 7)
        self.assertFalse(self.device.running)
        self.assertEqual(self.device.calls[0][:3], ("PUT", "/mode2", {"mode": 7}))

    def test_changing_running_speed_does_not_restart_playback(self):
        self.device.mode, self.device.running = 0, True
        self.assertEqual(handy.set_speed(40), {"running": True, "speed": 40})
        writes = [c[1] for c in self.device.calls if c[0] == "PUT"]
        self.assertEqual(writes, ["/hamp/velocity"])

    def test_maximum_zero_disables_motion(self):
        with patch.object(config, "HANDY_MIN_SPEED", 0), patch.object(config, "HANDY_MAX_SPEED", 0):
            with self.assertRaises(handy.HandyError):
                handy.set_speed(1)
            self.assertIn("movement commands are disabled", handy.section())
            handy.set_speed(0)

    def test_lost_connection_before_start_never_sends_motion(self):
        self.assertTrue(handy.available())
        self.device.connected = False
        with self.assertRaises(handy.HandyError):
            handy.set_speed(30)
        self.assertTrue(all(c[0] == "GET" for c in self.device.calls))

    def test_failed_start_attempts_stop_without_retry(self):
        self.device.fail["/hamp/start"] = TimeoutError("tokenSecret deviceSecret")
        result = tools._dispatch("set_speed", {"speed": 30})
        self.assertIn("device was stopped", result)
        self.assertEqual(sum(c[1] == "/hamp/start" for c in self.device.calls), 1)
        self.assertEqual(self.device.calls[-1][2], {"mode": 7})
        self.assertNotIn("tokenSecret", result)
        self.assertNotIn("deviceSecret", result)

    def test_failed_stop_is_never_reported_as_success(self):
        self.device.fail["/mode2"] = {"error": {"message": "deviceSecret tokenSecret"}}
        result = tools._dispatch("stop_device", {})
        self.assertTrue(result.startswith("("))
        self.assertIn("unconfirmed", result)
        self.assertNotIn("deviceSecret", result)
        self.assertNotIn("tokenSecret", result)

    def test_legacy_mode_acknowledgement_requires_readback(self):
        self.device.fail["/mode2"] = {"result": "ok"}
        self.device.mode = 7
        self.assertTrue(handy.stop_device()["stopped"])
        self.assertEqual(self.device.calls[-1][1], "/mode")
        self.device.mode = 0
        self.assertTrue(tools._dispatch("stop_device", {}).startswith("("))

    def test_invalid_motion_acknowledgement_stops(self):
        self.device.fail["/hamp/velocity"] = {"result": {"play_state": 0, "velocity": "invalid"}}
        result = tools._dispatch("set_speed", {"speed": 30})
        self.assertIn("device was stopped", result)
        self.assertFalse(any(c[1] == "/hamp/start" for c in self.device.calls))

    def test_zero_velocity_acknowledgements_do_not_stop_successful_start(self):
        self.device.replies["/hamp/velocity"] = [{"play_state": 0, "velocity": 0}]
        self.device.replies["/hamp/start"] = [{"play_state": 1, "velocity": 0}]
        self.assertEqual(handy.set_speed(50), {"running": True, "speed": 50})
        self.assertTrue(self.device.running)
        self.assertFalse(any(c[1] == "/mode2" and c[2] == {"mode": 7} for c in self.device.calls))

    def test_start_that_clears_velocity_finishes_at_the_requested_speed(self):
        self.device.reset_velocity_on_start = True
        self.assertEqual(handy.set_speed(50), {"running": True, "speed": 50})
        self.assertEqual(self.device.velocity, .5)
        self.assertTrue(self.device.running)
        start_index = next(i for i, c in enumerate(self.device.calls) if c[1] == "/hamp/start")
        self.assertEqual(self.device.calls[start_index + 1][:3], ("PUT", "/hamp/velocity", {"velocity": .5}))
        self.assertEqual(self.device.calls[-1][:2], ("GET", "/hamp/state"))

    def test_post_start_speed_failure_stops_without_retrying_start(self):
        original = self.device.__call__
        def fail_after_start(request, timeout):
            if request.full_url.endswith('/hamp/velocity') and self.device.running:
                raise TimeoutError('tokenSecret')
            return original(request, timeout)
        handy.urllib.request.urlopen.side_effect = fail_after_start
        result = tools._dispatch('set_speed', {'speed': 50})
        self.assertIn('device was stopped', result)
        self.assertFalse(self.device.running)
        self.assertEqual(sum(c[1] == '/hamp/start' for c in self.device.calls), 1)

    def test_different_reported_velocity_does_not_stop_acknowledged_speed_change(self):
        self.device.mode, self.device.running = 0, True
        self.device.replies["/hamp/velocity"] = [{"play_state": 1, "velocity": .2}]
        self.assertEqual(handy.set_speed(50), {"running": True, "speed": 50})
        self.assertEqual([c[1] for c in self.device.calls if c[0] == "PUT"], ["/hamp/velocity"])

    def test_non_fractional_state_velocity_is_preserved_without_guessing_speed(self):
        self.device.mode, self.device.running = 0, True
        self.device.velocity = 100
        status = handy.get_device_status()
        self.assertTrue(status["available"])
        self.assertIsNone(status["speed"])
        self.assertEqual(status["reported_velocity"], 100)
        self.assertNotIn("None%", handy.section())
        self.device.replies["/hamp/velocity"] = [{"play_state": 1, "velocity": 100}]
        self.assertEqual(handy.set_speed(50), {"running": True, "speed": 50})

    def test_delayed_playback_flag_is_verified_by_reads_without_restarting(self):
        self.device.replies["/hamp/start"] = [{"play_state": 0, "velocity": 0}]
        self.device.replies["/hamp/state"] = [{"play_state": 0, "velocity": 0}, {"play_state": 1, "velocity": 0}]
        self.assertEqual(handy.set_speed(50), {"running": True, "speed": 50})
        self.assertEqual(sum(c[1] == "/hamp/start" for c in self.device.calls), 1)
        self.assertEqual(sum(c[1] == "/hamp/state" for c in self.device.calls), 2)
        self.assertEqual(self.device.mode, 0)

    def test_start_that_never_becomes_playing_still_stops(self):
        self.device.replies["/hamp/start"] = [{"play_state": 0, "velocity": 0}]
        self.device.replies["/hamp/state"] = [{"play_state": 0, "velocity": 0}] * 3
        result = tools._dispatch("set_speed", {"speed": 50})
        self.assertIn("requested movement was not confirmed", result)
        self.assertIn("device was stopped", result)
        self.assertFalse(self.device.running)
        self.assertEqual(sum(c[1] == "/hamp/start" for c in self.device.calls), 1)

    def test_readback_timeout_after_start_still_stops(self):
        self.device.replies["/hamp/start"] = [{"play_state": 0, "velocity": 0}]
        self.device.fail["/hamp/state"] = TimeoutError("tokenSecret")
        result = tools._dispatch("set_speed", {"speed": 50})
        self.assertIn("device was stopped", result)
        self.assertFalse(self.device.running)
        self.assertNotIn("tokenSecret", result)

    def test_command_log_records_numeric_acknowledgements_without_credentials(self):
        self.use_application_key()
        self.device.replies["/hamp/start"] = [{"play_state": 1, "velocity": 0,
                                               "token": "leakedRemoteSecret", "mode_session_id": "deviceSecret"}]
        handy.set_speed(50)
        log = (self.saved.parent / "handy-events.jsonl").read_text()
        self.assertNotIn("Secret", log)
        self.assertNotIn("auth/token", log)
        entries = [json.loads(line) for line in log.splitlines()]
        writes = [e for e in entries if e["method"] == "PUT"]
        self.assertEqual([e["path"] for e in writes], ["/mode2", "/hamp/velocity", "/hamp/start", "/hamp/velocity"])
        self.assertEqual(writes[-2]["result"], {"play_state": 1, "velocity": 0})
        self.assertEqual(entries[-1]["path"], '/hamp/state')

    def test_command_log_shows_internal_stop_after_a_failed_start(self):
        self.device.fail["/hamp/start"] = TimeoutError("tokenSecret")
        tools._dispatch("set_speed", {"speed": 50})
        log = (self.saved.parent / "handy-events.jsonl").read_text()
        self.assertNotIn("tokenSecret", log)
        entries = [json.loads(line) for line in log.splitlines()]
        self.assertIn("error", entries[-2])
        self.assertEqual(entries[-1]["request"], {"mode": 7})

    def test_command_log_failure_does_not_stop_successful_motion(self):
        (self.saved.parent / "handy-events.jsonl").mkdir()
        self.assertEqual(handy.set_speed(50), {"running": True, "speed": 50})

    def test_commands_and_responses_are_printed_to_console(self):
        handy.set_speed(50)
        console = self.console.getvalue()
        self.assertIn('[Handy]', console)
        self.assertIn('"path": "/hamp/velocity"', console)
        self.assertIn('"velocity": 0.5', console)
        self.assertIn(f'"revision": "{handy.REVISION}"', console)
        self.assertNotIn('Secret', console)

    def test_file_log_failure_is_visible_in_console_without_affecting_motion(self):
        (self.saved.parent / "handy-events.jsonl").mkdir()
        self.assertEqual(handy.set_speed(50), {"running": True, "speed": 50})
        self.assertIn('command log could not be written', self.console.getvalue())

    def test_api_http_error_and_malformed_responses_hide_device(self):
        for failure in ({"error": {"message": "deviceSecret"}}, {"result": {}},
                        urllib.error.HTTPError(handy.API_URL, 401, "tokenSecret", {}, None)):
            self.device.fail["/connected"] = failure
            handy.invalidate()
            self.assertEqual(assemble.handy_section(), "")
            status = handy.get_device_status()
            self.assertFalse(status["available"])
            self.assertNotIn("Secret", json.dumps(status))

    def test_small_tiny_and_explicit_kits_and_voice(self):
        for kit in ("small", "tiny", "full"):
            with patch.object(config, "TOOL_KIT", kit):
                self.assertTrue(handy.TOOL_NAMES <= self.names())
                self.assertTrue(handy.TOOL_NAMES <= {d["function"]["name"] for d in voice_initiative.voice_tools()})
        with patch.object(config, "TOOL_KIT", ["do_nothing"]):
            self.assertTrue(handy.TOOL_NAMES.isdisjoint(self.names()))
            self.assertEqual(assemble.handy_section(), "")

    def test_immediate_context_updates_without_credentials(self):
        with patch.object(assemble, "retrieved_ids", return_value=("fixture memories", [])), patch.object(assemble, "body_pulse", return_value=""):
            block, _ = assemble.moment("hello")
            self.assertIn("The Handy 2", block)
            self.assertNotIn("Secret", block)
            self.device.connected = False
            handy.invalidate()
            block, _ = assemble.moment("hello")
            self.assertNotIn("Handy", block)
            self.assertNotIn("set_speed", block)

    def test_http_auth_headers_and_timeout(self):
        handy.get_device_status()
        request = handy.urllib.request.urlopen.call_args.args[0]
        self.assertEqual(request.get_header("X-connection-key"), "deviceSecret")
        self.assertEqual(request.get_header("Authorization"), "Bearer tokenSecret")
        self.assertEqual(self.device.calls[-1][3], 3)

    def test_panel_secrets_preserve_other_field_and_are_never_echoed(self):
        first = panel.secret("handy_connection_key", "newDeviceKey")
        second = panel.secret("handy_api_token", "newTokenSecret")
        self.assertTrue(first["ok"] and second["ok"])
        saved = json.loads(self.saved.read_text())
        self.assertEqual(saved, {"connection_key": "newDeviceKey", "api_token": "newTokenSecret"})
        self.assertNotIn("newDeviceKey", json.dumps(first))
        self.assertNotIn("newTokenSecret", json.dumps(second))
        self.assertTrue(panel.secrets_set()["handy_connection_key"])
        self.assertTrue(panel.secrets_set()["handy_api_token"])
        self.assertFalse(panel.secret("handy_connection_key", "bad-key")["ok"])

    def test_environment_overrides_saved_credentials_and_invalidates_cache(self):
        handy.available()
        count = len(self.device.calls)
        with patch.dict(os.environ, {"HANDY_CONNECTION_KEY": "envDevice", "HANDY_API_TOKEN": "envToken"}):
            handy.available()
            self.assertGreater(len(self.device.calls), count)
            request = handy.urllib.request.urlopen.call_args.args[0]
            self.assertEqual(request.get_header("Authorization"), "Bearer envToken")


if __name__ == "__main__":
    unittest.main()
