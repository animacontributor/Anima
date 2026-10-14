package local.anima.phone;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/** Native playback contracts runnable on the JDK without Android or a WebView. */
public final class NativePlaybackContract {
    static void check(boolean value,String message){if(!value)throw new AssertionError(message);}
    static byte[] wav(int bits){
        int bytes=bits/8;ByteBuffer b=ByteBuffer.allocate(44+4*bytes).order(ByteOrder.LITTLE_ENDIAN);
        b.putInt(0x46464952).putInt(b.capacity()-8).putInt(0x45564157);
        b.putInt(0x20746d66).putInt(16).putShort((short)1).putShort((short)2).putInt(48000).putInt(48000*2*bytes).putShort((short)(2*bytes)).putShort((short)bits);
        b.putInt(0x61746164).putInt(4*bytes);
        for(int v:new int[]{-1,1,-1,1}){int sample=v*(bits==16?16384:4194304);if(bits==16)b.putShort((short)sample);else b.put((byte)sample).put((byte)(sample>>8)).put((byte)(sample>>16));}
        return b.array();
    }
    static class Host implements NativeVoicePlayback.Host {
        final List<Integer> acks=new ArrayList<>();final CountDownLatch delivered=new CountDownLatch(1);
        int completed;boolean partial;
        public NativeVoicePlayback.Status status(String j,String v,int next)throws Exception{NativeVoicePlayback.Status s=new NativeVoicePlayback.Status();s.available=next<2;s.done=next>=2;return s;}
        public byte[] audio(String j,String v,int i)throws Exception{return wav(24);}
        public void acknowledge(String j,String v,int i){acks.add(i);}
        public void delivered(String j,String v,int count,boolean p,String reason){completed=count;partial=p;delivered.countDown();}
        public float volume(){return .7f;}
    }
    static class Sink implements NativeVoicePlayback.Sink {
        final List<Float> output=new ArrayList<>();long played;boolean stopped;
        public void write(float[] samples,float volume)throws Exception{check(volume==.7f,"Volume changed");for(float f:samples)output.add(f);played+=samples.length/2;}
        public long played(){return played;}
        public void stop(){stopped=true;}
        public void close(){}
    }
    public static void main(String[] args)throws Exception {
        for(int bits:new int[]{16,24}){
            PcmWave w=PcmWave.decode(wav(bits));check(w.channels==2&&w.rate==48000,"Stereo format lost");
            check(w.samples[0]==-.5f&&w.samples[1]==.5f&&w.samples.length==4,"PCM or ear order changed");
        }
        Host h=new Host();Sink sink=new Sink();NativeVoicePlayback p=new NativeVoicePlayback(h,(r,c)->{check(r==48000&&c==2,"Wrong sink format");return sink;});
        check(p.start("job","visit",p.epoch()),"Start failed");check(h.delivered.await(2,TimeUnit.SECONDS),"Playback depends on UI callbacks");
        check(h.completed==2&&h.acks.size()==2&&!h.partial,"Incorrect playback receipts");check(sink.output.size()==8,"Audio missing or duplicated");
        for(int i=0;i<8;i++)check(sink.output.get(i)==(i%2==0?-.5f:.5f),"Stereo samples changed");
        int old=p.epoch();p.stop();check(!p.start("stale","visit",old),"Stale turn resurrected playback");

        CountDownLatch downloading=new CountDownLatch(1),release=new CountDownLatch(1);int[] opened={0};
        Host delayed=new Host(){public byte[] audio(String j,String v,int i)throws Exception{downloading.countDown();release.await(2,TimeUnit.SECONDS);return super.audio(j,v,i);}};
        NativeVoicePlayback cancelled=new NativeVoicePlayback(delayed,(r,c)->{opened[0]++;return new Sink();});
        cancelled.start("waiting","visit",cancelled.epoch());check(downloading.await(2,TimeUnit.SECONDS),"Download not reached");cancelled.stop();release.countDown();
        check(delayed.delivered.await(2,TimeUnit.SECONDS),"Cancelled worker did not exit");check(opened[0]==0&&delayed.completed==0,"Late download started muted audio");

        CountDownLatch writing=new CountDownLatch(1),unblock=new CountDownLatch(1);
        Host interrupted=new Host();Sink blocked=new Sink(){public void write(float[] a,float volume)throws Exception{writing.countDown();unblock.await(2,TimeUnit.SECONDS);}public void stop(){super.stop();unblock.countDown();}};
        NativeVoicePlayback active=new NativeVoicePlayback(interrupted,(r,c)->blocked);active.start("playing","visit",active.epoch());
        check(writing.await(2,TimeUnit.SECONDS),"Write not reached");active.stop();check(interrupted.delivered.await(2,TimeUnit.SECONDS),"Interrupt did not exit");
        check(blocked.stopped&&interrupted.completed==0&&interrupted.partial,"Interrupted audio incorrectly confirmed");
        System.out.println("PASS: independent native playback, PCM16/24 stereo, volume, receipts, stale turns, late downloads and interruption.");
    }
}
