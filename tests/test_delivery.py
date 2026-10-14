"""Delivery controls, bounded scripts, and checked synthesis without model downloads."""
from pathlib import Path
import contextlib,io,sys,threading,unittest
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
import delivery,kyutai_voice

class SettingsTests(unittest.TestCase):
    def test_live_parlor_bypasses_saved_tempo_and_blend_settings(self):
        with patch.object(delivery.config,'LIVE_VOICE_KYUTAI_SPEED',.8),patch.object(delivery.config,'LIVE_VOICE_KYUTAI_WHISPER_MIX',.6):
            self.assertEqual(delivery.live()['speed'],1)
            self.assertEqual(delivery.live()['whisper_mix'],0)
    def test_bounds_finite_values_and_guidance_steps(self):
        self.assertEqual(delivery.clean({'speed':.2,'whisper_mix':9,'pause_ms':2000,'guidance':2.5}),{'speed':.75,'whisper_mix':1,'pause_ms':600,'guidance':2.5})
        for value in ({'speed':float('nan')},{'whisper_mix':float('inf')},{'guidance':2.2},{'speed':None},{'pause_ms':'bad'},[]):
            with self.assertRaises(ValueError):delivery.clean(value)

    def test_sections_preserve_words_and_bound_first_and_later_requests(self):
        text=' '.join(f'This is sentence {i}, with enough words to exercise boundaries.' for i in range(60))
        parts=delivery.sections(text)
        self.assertEqual(' '.join(parts).split(),text.split())
        self.assertLessEqual(len(parts[0]),150)
        self.assertTrue(all(len(p)<=300 and len(p.split())<=42 for p in parts))
        # A cut at an earlier clause must still bound the words remaining after it.
        text='One sentence. '+' '.join(['abcdefghijklmnopqrst']*160)
        self.assertTrue(all(len(p)<=300 for p in delivery.sections(text)))
        self.assertEqual(delivery.sections(' \n '),[])
        with self.assertRaises(ValueError):delivery.sections('x'*301)

class ModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import numpy,torch
            from moshi.conditioners import ConditionAttributes,TensorCondition
        except ImportError:raise unittest.SkipTest('Run model contracts in the local Kyutai environment.')
        cls.np=numpy;cls.torch=torch;cls.Attributes=ConditionAttributes;cls.Tensor=TensorCondition

    def adapter(self,failures=0,loop=False,frame_count=3):
        np=self.np;torch=self.torch;counts={'calls':0,'contexts':0,'scripts':[]}
        @contextlib.contextmanager
        def streaming(batch):
            counts['contexts']+=1;yield
        def decode(frame):
            bad=counts['calls']<=failures
            return torch.tensor(np.full((1,1,20),np.nan if bad else .1,dtype=np.float32))
        def generate(entries,condition,on_frame):
            counts['calls']+=1
            counts['finished']=False
            for i in range(12 if loop else frame_count):on_frame(torch.full((1,33,1),5 if loop else i))
            counts['finished']=True
        model=SimpleNamespace(mimi=SimpleNamespace(streaming=streaming,decode=decode),
            prepare_script=lambda text,**k:counts['scripts'].append(text[0]) or text,generate=generate)
        voice=kyutai_voice.KyutaiVoice.__new__(kyutai_voice.KyutaiVoice);voice.model=model;voice.sample_rate=200;voice.conditions={}
        voice.condition=lambda *a:None
        return voice,counts

    def test_bad_section_retried_before_any_audio_is_released(self):
        voice,counts=self.adapter(failures=1);chunks=[]
        with patch.object(sys,'stderr',io.StringIO()):
            voice.synthesize('A small neutral test.','desire',delivery={'speed':1},on_audio=lambda a,r:chunks.append(a.copy()))
        self.assertEqual(counts['calls'],2);self.assertEqual(counts['contexts'],2)
        self.assertTrue(chunks);self.assertTrue(all(self.np.isfinite(a).all() for a in chunks))

    def test_two_bad_attempts_and_repeated_codes_are_never_emitted(self):
        for voice,counts in (self.adapter(failures=2),self.adapter(loop=True)):
            chunks=[]
            with patch.object(sys,'stderr',io.StringIO()),self.assertRaises(kyutai_voice.AudioQualityError):
                voice.synthesize('A test.',delivery={'speed':.9},on_audio=lambda a,r:chunks.append(a))
            self.assertEqual(counts['calls'],2);self.assertEqual(chunks,[])

    def test_every_section_gets_fresh_generation_and_cancellation_stops_remaining_sections(self):
        voice,counts=self.adapter();text=' '.join(['An ordinary sentence with a few clear words.']*30)
        with patch.object(kyutai_voice.config,'LIVE_VOICE_SPLIT_REPLIES',True):
            voice.synthesize(text,delivery={'speed':1,'pause_ms':0})
        self.assertEqual(counts['contexts'],len(delivery.sections(text)))
        voice,counts=self.adapter();cancel=threading.Event();chunks=[]
        def receive(a,r):chunks.append(a);cancel.set()
        with patch.object(kyutai_voice.config,'LIVE_VOICE_SPLIT_REPLIES',True),self.assertRaises(InterruptedError):
            voice.synthesize(text,delivery={'speed':1},cancel=cancel,on_audio=receive)
        self.assertEqual(counts['calls'],1);self.assertEqual(len(chunks),1)

    def test_whole_reply_uses_one_generation_across_paragraphs_and_transport_packets(self):
        voice,counts=self.adapter(frame_count=12)
        text='\n\n'.join(['An ordinary sentence with a few clear words.']*30)
        chunks=[]
        with patch.object(kyutai_voice.config,'LIVE_VOICE_SPLIT_REPLIES',False):
            voice.synthesize(text,delivery={'speed':1},on_audio=lambda a,r:chunks.append(a.copy()))
        self.assertEqual(counts['calls'],1)
        self.assertEqual(counts['contexts'],1)
        self.assertEqual(counts['scripts'],[text])
        self.assertGreater(len(chunks),1)
        voice,counts=self.adapter(frame_count=12);cancel=threading.Event();chunks=[]
        def receive(a,r):chunks.append(a);cancel.set()
        with patch.object(kyutai_voice.config,'LIVE_VOICE_SPLIT_REPLIES',False),self.assertRaises(InterruptedError):
            voice.synthesize(text,delivery={'speed':1},cancel=cancel,on_audio=receive)
        self.assertEqual(counts['calls'],1)
        self.assertEqual(len(chunks),1)

    def test_live_pcm_arrives_before_generation_finishes_without_tempo_processing(self):
        voice,counts=self.adapter(frame_count=12);chunks=[]
        def receive(a,r):
            chunks.append(a.copy())
            self.assertFalse(counts['finished'])
        with patch.object(kyutai_voice,'stretch',side_effect=AssertionError('Live playback must not stretch')):
            voice.synthesize('A continuous reply.',delivery={'speed':1},on_audio=receive)
        self.assertGreater(len(chunks),1)
        self.assertEqual(counts['calls'],1)
        self.assertEqual(sum(map(len,chunks)),240)

    def test_live_error_after_first_audio_does_not_replay_the_audible_prefix(self):
        voice,counts=self.adapter(loop=True);chunks=[]
        with patch.object(sys,'stderr',io.StringIO()),self.assertRaises(kyutai_voice.AudioQualityError):
            voice.synthesize('A test.',delivery={'speed':1},on_audio=lambda a,r:chunks.append(a))
        self.assertEqual(counts['calls'],1)
        self.assertTrue(chunks)
        self.assertTrue(all(self.np.isfinite(a).all() for a in chunks))

    def test_reference_blend_is_one_speaker_and_does_not_mutate_cached_source(self):
        torch=self.torch;original=self.Attributes(tensor={'speaker_wavs':self.Tensor(torch.zeros(1,4,8),torch.ones(1,4))})
        whisper=self.Attributes(tensor={'speaker_wavs':self.Tensor(torch.ones(1,4,8),torch.ones(1,4))})
        voice=kyutai_voice.KyutaiVoice.__new__(kyutai_voice.KyutaiVoice);voice.conditions={}
        voice.model=SimpleNamespace(get_voice_path=lambda x:x,make_condition_attributes=lambda paths,**k:whisper if 'whisper' in paths[0] else original)
        mixed=voice.condition('base',.3,2)
        torch.testing.assert_close(mixed.tensor['speaker_wavs'].tensor,torch.full((1,4,8),.3))
        self.assertTrue(torch.equal(original.tensor['speaker_wavs'].tensor,torch.zeros(1,4,8)))
        self.assertIs(voice.condition('base',1,2),whisper)
        self.assertIs(voice.condition('base',0,2),original)

if __name__=='__main__':unittest.main()
