package local.anima.phone;

import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.ArrayDeque;
import java.util.Arrays;

/** Local utterance boundaries and WAV packaging, independent of the WebView. */
public final class NativeVoiceCapture {
    public interface Listener { void started(); void utterance(byte[] wave); }
    private final Listener listener;
    public volatile double threshold=.0092;
    public volatile int silenceMs=1000,minSpeechMs=160,interruptMs=350,maxSeconds=30;
    private double noise=.002,candidate,gap,age,voiced,silence;
    private boolean active;
    private final ArrayDeque<short[]> pre=new ArrayDeque<>();
    private int preSize;
    private final ByteArrayOutputStream pcm=new ByteArrayOutputStream();
    public NativeVoiceCapture(Listener listener){this.listener=listener;}
    public synchronized boolean active(){return active;}
    public synchronized boolean pending(){return active||candidate>0;}
    public synchronized void reset(){pre.clear();preSize=0;pcm.reset();candidate=gap=age=voiced=silence=0;active=false;}
    private void append(short[] samples){for(short sample:samples){pcm.write(sample&255);pcm.write((sample>>8)&255);}}
    public synchronized void push(short[] samples,int count,boolean speaking,boolean blocked){
        if(blocked){reset();return;}
        if(count<=0)return;
        double energy=0;for(int i=0;i<count;i++){double sample=samples[i]/32768.0;energy+=sample*sample;}
        double level=Math.sqrt(energy/count),ms=count/16.0;
        if(!active&&!speaking&&level<threshold)noise=noise*.98+level*.02;
        boolean loud=level>Math.max(threshold,noise*3);
        short[] frame=Arrays.copyOf(samples,count);
        if(!active){
            pre.add(frame);preSize+=count;
            int lead=Math.max(1000,Math.max(minSpeechMs,interruptMs)+300)*16;
            while(preSize>lead&&pre.size()>1)preSize-=pre.remove().length;
            if(candidate>0)age+=ms;
            if(age>900){candidate=age=0;}
            if(loud){candidate+=ms;gap=0;}else{gap+=ms;if(gap>140){candidate=age=0;}}
            int confirm=Math.max(minSpeechMs,speaking?interruptMs:minSpeechMs);
            if(!loud||candidate<confirm)return;
            active=true;for(short[] previous:pre)append(previous);voiced=candidate;silence=0;
            listener.started();return;
        }
        append(frame);
        if(loud){voiced+=ms;silence=0;}else silence+=ms;
        if(silence>=silenceMs||pcm.size()/2>=maxSeconds*16000){
            if(voiced>=minSpeechMs){
                byte[] bytes=pcm.toByteArray();ByteBuffer wave=ByteBuffer.allocate(44+bytes.length).order(ByteOrder.LITTLE_ENDIAN);
                wave.putInt(0x46464952).putInt(36+bytes.length).putInt(0x45564157).putInt(0x20746d66).putInt(16);
                wave.putShort((short)1).putShort((short)1).putInt(16000).putInt(32000).putShort((short)2).putShort((short)16);
                wave.putInt(0x61746164).putInt(bytes.length).put(bytes);listener.utterance(wave.array());
            }
            reset();
        }
    }
}
