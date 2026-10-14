"""Desktop DSP invariants; no microphone, TTS/LLM or personal audio."""
import base64
import io
from pathlib import Path
import sys
import unittest
import wave
import numpy as np
from scipy import signal
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'engine'))
import spatial_worker as dsp

class StreamTests(unittest.TestCase):
    def test_streaming_matches_whole_signal_at_supported_rates(self):
        rng=np.random.default_rng(23);kernel=rng.normal(0,.0008,(2,14000));kernel[:,0]=.35;source=rng.normal(0,.02,12001)
        for rate in (16000,24000,44100,48000):
            with self.subTest(rate=rate):
                whole=dsp.Stream(kernel);expected=np.concatenate([whole.process(source,rate),whole.finish()],axis=1)
                chunked=dsp.Stream(kernel);parts=[chunked.process(chunk,rate) for chunk in np.array_split(source,17)];parts.append(chunked.finish());actual=np.concatenate(parts,axis=1)
                self.assertEqual(actual.shape,expected.shape);np.testing.assert_allclose(actual,expected,atol=1e-12)
    def test_silent_input_is_silent_and_limiter_keeps_ear_ratio(self):
        kernel=np.array([[4.,0],[2.,0]]);stream=dsp.Stream(kernel);out=stream.process(np.ones(30),48000)
        self.assertLessEqual(np.max(np.abs(out)),.97000001);np.testing.assert_allclose(out[0],out[1]*2)
        self.assertTrue(np.isfinite(out).all());self.assertGreater(stream.limited,0)
        silence=dsp.Stream(kernel).process(np.zeros(30),48000);self.assertEqual(np.count_nonzero(silence),0)
    def test_stereo_24bit_wav_and_24bit_source_are_preserved(self):
        raw=dsp.wav(np.array([[.5,-.5],[.25,-.25]]))
        with wave.open(io.BytesIO(raw)) as f:self.assertEqual(f.getnchannels(),2);self.assertEqual(f.getframerate(),48000);self.assertEqual(f.getsampwidth(),3)
        b=io.BytesIO()
        with wave.open(b,'wb') as f:f.setnchannels(1);f.setsampwidth(3);f.setframerate(24000);f.writeframes(bytes([0,0,64,0,0,192]))
        samples,rate=dsp.readwav(b.getvalue());np.testing.assert_array_equal(samples,[.5,-.5]);self.assertEqual(rate,24000)
    def test_nonfinite_settings_and_unknown_profile_are_rejected(self):
        for values in ({'distance':float('nan')},{'azimuth':float('inf')},{'profile':'unknown'}):
            with self.assertRaises(ValueError):dsp.clean(values)

@unittest.skipUnless((dsp.DATA/'ku100-150.sofa').exists() and (dsp.DATA/'sadie-d2.sofa').exists(),'optional pinned measured responses not installed')
class MeasuredTests(unittest.TestCase):
    def test_cardinal_ear_order_and_near_field_strength_for_both_profiles(self):
        for profile in ('ku100','sadie'):
            responses=dsp.Responses(profile)
            for side in (-90,90):
                near,_=responses.prepare({'enabled':True,'profile':profile,'azimuth':side,'distance':.11,'room':0})
                far,_=responses.prepare({'enabled':True,'profile':profile,'azimuth':side,'distance':1,'room':0})
                close=0 if side<0 else 1;other=1-close
                near_ratio=np.sum(near[close]**2)/np.sum(near[other]**2);far_ratio=np.sum(far[close]**2)/np.sum(far[other]**2)
                self.assertGreater(far_ratio,3,(profile,side));self.assertGreater(near_ratio,far_ratio*4,(profile,side))
                self.assertTrue(np.isfinite(near).all())
    def test_room_adds_a_quiet_late_response_without_changing_the_direct_path(self):
        responses=dsp.Responses();dry,_=responses.prepare({'enabled':True,'azimuth':90,'distance':.11,'room':0});room,info=responses.prepare({'enabled':True,'azimuth':90,'distance':.11,'room':.06})
        np.testing.assert_allclose(dry[:,:300],room[:,:300],atol=1e-12)
        self.assertGreater(np.sum(room[:,4000:]**2),0);self.assertLess(np.sum(room[:,4000:]**2),np.sum(room[:,:2000]**2)*.005)
        self.assertEqual(info['nearest_ear_cm'],2)

if __name__=='__main__':unittest.main()
