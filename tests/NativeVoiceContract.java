package local.anima.phone;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicBoolean;

/** Screen-off contracts: native events, capture and playback with no WebView. */
public final class NativeVoiceContract {
    static void check(boolean condition,String message){if(!condition)throw new AssertionError(message);}
    static NativeVoiceEvents.Batch batch(){
        NativeVoiceEvents.Batch result=new NativeVoiceEvents.Batch();result.cursor=1;
        result.events.add(new NativeVoiceEvents.Event(1,"continuation","A story while locked."));return result;
    }
    static void pendingInput(boolean accepted)throws Exception{
        AtomicInteger epoch=new AtomicInteger(),starts=new AtomicInteger(),discarded=new AtomicInteger();
        AtomicBoolean pending=new AtomicBoolean(true);
        CountDownLatch held=new CountDownLatch(1),delivered=new CountDownLatch(1);
        NativeVoiceEvents events=new NativeVoiceEvents(new NativeVoiceEvents.Host(){
            public int outputEpoch(){return epoch.get();}
            public boolean inputPending(){held.countDown();return pending.get();}
            public NativeVoiceEvents.Batch poll(long after){return batch();}
            public boolean play(NativeVoiceEvents.Event event,int expected){
                if(expected!=epoch.get())return false;
                check(!pending.get(),"Initiative played during capture or recognition");starts.incrementAndGet();return true;
            }
            public void received(NativeVoiceEvents.Event event){delivered.countDown();}
            public void discard(NativeVoiceEvents.Event event){discarded.incrementAndGet();delivered.countDown();}
            public void ended(){}
        },0,10);
        events.start();
        try{
            check(held.await(2,TimeUnit.SECONDS),"Event was not held while input pending");
            check(starts.get()==0&&discarded.get()==0,"Pending input interrupted or played initiative");
            if(accepted)epoch.incrementAndGet();pending.set(false);
            check(delivered.await(2,TimeUnit.SECONDS),"Pending event was never resolved");
            check(starts.get()==(accepted?0:1),"Accepted input did not supersede initiative, or noise lost it");
            check(discarded.get()==(accepted?1:0),"Wrong initiative discard after recognition");
        }finally{events.stop();}
    }
    static void inputDuringDownload(boolean accepted)throws Exception{
        AtomicBoolean pending=new AtomicBoolean(false);CountDownLatch checked=new CountDownLatch(1);
        NativePlaybackContract.Host host=new NativePlaybackContract.Host(){
            public byte[] audio(String job,String visit,int index)throws Exception{pending.set(true);return super.audio(job,visit,index);}
        };
        AtomicInteger opened=new AtomicInteger();
        NativeVoicePlayback playback=new NativeVoicePlayback(host,(rate,channels)->{opened.incrementAndGet();return new NativePlaybackContract.Sink();});
        playback.start("initiative","visit",playback.epoch(),()->{checked.countDown();return pending.get();});
        check(checked.await(2,TimeUnit.SECONDS),"Input arriving during audio download was not checked");
        check(opened.get()==0,"Initiative started output over pending input");
        if(accepted)playback.stop();pending.set(false);
        check(host.delivered.await(2,TimeUnit.SECONDS),"Held playback did not resolve");
        check(opened.get()==(accepted?0:1),"Accepted input resurrected output, or rejected noise lost output");
        check(host.completed==(accepted?0:2),"Held initiative delivery receipt was wrong");
    }
    public static void main(String[] args)throws Exception {
        NativePlaybackContract.Host audio=new NativePlaybackContract.Host();NativePlaybackContract.Sink sink=new NativePlaybackContract.Sink();
        NativeVoicePlayback playback=new NativeVoicePlayback(audio,(rate,channels)->sink);
        AtomicInteger polls=new AtomicInteger(),starts=new AtomicInteger(),observed=new AtomicInteger();
        CountDownLatch repeated=new CountDownLatch(1);
        NativeVoiceEvents events=new NativeVoiceEvents(new NativeVoiceEvents.Host(){
            public int outputEpoch(){return playback.epoch();}
            public NativeVoiceEvents.Batch poll(long after)throws Exception{
                if(polls.incrementAndGet()==1)throw new java.io.IOException("Transient LAN failure");
                if(after==1)repeated.countDown();return batch();
            }
            public boolean play(NativeVoiceEvents.Event event,int expected){starts.incrementAndGet();return playback.start(event.job,"visit",expected);}
            public void received(NativeVoiceEvents.Event event){observed.incrementAndGet();}
            public void discard(NativeVoiceEvents.Event event){throw new AssertionError("Fresh event discarded");}
            public void ended(){}
        },0,10);
        events.start();check(audio.delivered.await(2,TimeUnit.SECONDS),"Native delivery depended on UI polling");
        check(repeated.await(2,TimeUnit.SECONDS),"Cursor was not advanced");events.stop();
        check(starts.get()==1&&observed.get()==1,"Duplicate event replayed speech");
        check(audio.completed==2&&sink.output.size()==8,"Native audio or delivery receipt missing");
        int output=sink.output.size();check(playback.start("continuation","visit",playback.epoch()),"Idempotent event start failed");
        check(sink.output.size()==output,"Completed job played twice");

        AtomicInteger epoch=new AtomicInteger(),discarded=new AtomicInteger();CountDownLatch polling=new CountDownLatch(1),release=new CountDownLatch(1),done=new CountDownLatch(1);
        NativeVoiceEvents stale=new NativeVoiceEvents(new NativeVoiceEvents.Host(){
            public int outputEpoch(){return epoch.get();}
            public NativeVoiceEvents.Batch poll(long after)throws Exception{polling.countDown();release.await(2,TimeUnit.SECONDS);return batch();}
            public boolean play(NativeVoiceEvents.Event event,int expected){return expected==epoch.get();}
            public void received(NativeVoiceEvents.Event event){throw new AssertionError("Manual interruption was overridden");}
            public void discard(NativeVoiceEvents.Event event){discarded.incrementAndGet();done.countDown();}
            public void ended(){}
        },0,10);
        stale.start();check(polling.await(2,TimeUnit.SECONDS),"Poll did not start");epoch.incrementAndGet();release.countDown();
        check(done.await(2,TimeUnit.SECONDS),"Stale event was not discarded");stale.stop();check(discarded.get()==1,"Stale event repeated");
        pendingInput(true);pendingInput(false);
        inputDuringDownload(true);inputDuringDownload(false);

        List<byte[]> recordings=new ArrayList<>();AtomicInteger candidates=new AtomicInteger();
        NativeVoiceCapture capture=new NativeVoiceCapture(new NativeVoiceCapture.Listener(){
            public void started(){candidates.incrementAndGet();}
            public void utterance(byte[] wave){recordings.add(wave);}
        });
        short[] loud=new short[320],quiet=new short[320];java.util.Arrays.fill(loud,(short)5000);
        capture.push(loud,320,false,false);check(capture.pending()&&!capture.active(),"Unfinished speech candidate was invisible to initiative delivery");capture.reset();
        for(int i=0;i<6;i++)capture.push(loud,320,false,false);
        for(int i=0;i<20;i++)capture.push(quiet,320,false,false);
        check(candidates.get()==0&&recordings.isEmpty(),"Brief noise submitted a prompt");
        for(int i=0;i<17;i++)capture.push(loud,320,true,false);check(candidates.get()==0,"Capture confirmation ignored");
        capture.push(loud,320,true,false);check(candidates.get()==1,"Native candidate was not detected");
        for(int i=0;i<51;i++)capture.push(quiet,320,true,false);
        check(recordings.size()==1,"Native phrase depended on a JS timer");PcmWave wave=PcmWave.decode(recordings.get(0));
        check(wave.channels==1&&wave.rate==16000&&wave.samples.length>16000,"Capture format or pre-roll changed");
        for(int i=0;i<100;i++)capture.push(loud,320,true,true);
        check(candidates.get()==1&&recordings.size()==1,"Speaker playback became microphone input");
        System.out.println("PASS: native continuation polling, retry/cursors, independent audio/receipts, interruption fencing, native capture and speaker gating without WebView.");
    }
}
