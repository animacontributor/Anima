package local.anima.phone;

import java.util.ArrayDeque;
import java.util.function.BooleanSupplier;

/** Owns polling, audio delivery and receipts independently of the WebView. */
public final class NativeVoicePlayback {
    public interface Host {
        Status status(String job,String visit,int next) throws Exception;
        byte[] audio(String job,String visit,int index) throws Exception;
        void acknowledge(String job,String visit,int index) throws Exception;
        void delivered(String job,String visit,int completed,boolean partial,String reason) throws Exception;
        float volume();
    }
    public interface Sink {
        void write(float[] samples,float volume) throws Exception;
        long played();
        void stop();
        void close();
    }
    public interface Factory { Sink open(int rate,int channels) throws Exception; }
    public static final class Status {
        public boolean available,done,cancelled;
        public String error="";
    }
    public static final class Job {
        public final String id,visit;
        public volatile boolean cancelled,done,started;
        public volatile boolean receipted;
        public volatile int completed;
        public volatile String error="";
        volatile Sink sink;
        final BooleanSupplier inputPending;
        Job(String id,String visit,BooleanSupplier inputPending){this.id=id;this.visit=visit;this.inputPending=inputPending;}
    }
    private final Host host;
    private final Factory factory;
    private int epoch;
    private Job current;
    public NativeVoicePlayback(Host h,Factory f){host=h;factory=f;}
    public synchronized int epoch(){return epoch;}
    public synchronized Job job(String id){return current!=null&&current.id.equals(id)?current:null;}
    public synchronized Job current(){return current;}
    public synchronized boolean playing(){return current!=null&&current.started&&!current.done&&!current.cancelled;}
    public synchronized boolean start(String id,String visit,int expected){
        return start(id,visit,expected,null);
    }
    public synchronized boolean start(String id,String visit,int expected,BooleanSupplier inputPending){
        if(expected!=epoch)return false;
        if(current!=null&&current.id.equals(id)&&current.visit.equals(visit))return !current.cancelled;
        stop();Job job=new Job(id,visit,inputPending);current=job;
        Thread thread=new Thread(()->run(job),"Anima-native-playback");thread.setDaemon(true);thread.start();return true;
    }
    public synchronized void stop(){
        epoch++;
        if(current!=null){current.cancelled=true;if(current.sink!=null)current.sink.stop();}
    }
    private void run(Job job){
        int next=0,rate=0,channels=0;long written=0;
        ArrayDeque<long[]> pending=new ArrayDeque<>();
        try{
            while(!job.cancelled){
                ack(job,pending);
                Status state=host.status(job.id,job.visit,next);
                if(job.cancelled)break;
                if(state.cancelled){job.cancelled=true;break;}
                if(state.available){
                    PcmWave wave=PcmWave.decode(host.audio(job.id,job.visit,next));
                    // Input may start during the first audio download. Only
                    // unsolicited speech waits; accepted replies play normally.
                    while(!job.started&&!job.cancelled&&job.inputPending!=null&&job.inputPending.getAsBoolean())Thread.sleep(10);
                    synchronized(this){
                        if(job.cancelled)break;
                        if(job.sink==null){rate=wave.rate;channels=wave.channels;job.sink=factory.open(rate,channels);}
                    }
                    if(wave.rate!=rate||wave.channels!=channels)throw new Exception("Voice format changed within a reply.");
                    job.started=true;job.sink.write(wave.samples,host.volume());
                    written+=wave.samples.length/channels;pending.add(new long[]{next++,written});
                    continue;
                }
                if(!state.error.isEmpty())throw new Exception(state.error);
                if(state.done){
                    while(!job.cancelled&&!pending.isEmpty()){ack(job,pending);Thread.sleep(20);}
                    break;
                }
                Thread.sleep(35);
            }
        }catch(Exception e){if(!job.cancelled)job.error=e.getMessage()==null?"Native audio playback failed.":e.getMessage();}
        finally{
            if(job.sink!=null){job.sink.stop();job.sink.close();}
            job.done=true;
            try{host.delivered(job.id,job.visit,job.completed,job.started&&!pending.isEmpty(),job.cancelled?"native playback interrupted":job.error.isEmpty()?"native playback completed":"native playback error");}catch(Exception ignored){}finally{job.receipted=true;}
        }
    }
    private void ack(Job job,ArrayDeque<long[]> pending) throws Exception {
        if(job.cancelled||job.sink==null)return;
        long played=job.sink.played();
        while(!pending.isEmpty()&&played>=pending.peek()[1]){
            long[] item=pending.remove();job.completed++;host.acknowledge(job.id,job.visit,(int)item[0]);
        }
    }
}
