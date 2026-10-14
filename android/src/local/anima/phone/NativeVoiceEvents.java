package local.anima.phone;

import java.util.ArrayList;
import java.util.List;

/** Native foreground-service delivery. No WebView, JavaScript or screen timer. */
public final class NativeVoiceEvents {
    public static final class Event {
        public final long sequence;
        public final String job,reply;
        public Event(long sequence,String job,String reply){this.sequence=sequence;this.job=job;this.reply=reply;}
    }
    public static final class Batch {
        public long cursor;
        public boolean voice=true;
        public final List<Event> events=new ArrayList<>();
    }
    public interface Host {
        int outputEpoch();
        default boolean inputPending(){return false;}
        Batch poll(long after) throws Exception;
        boolean play(Event event,int expectedEpoch) throws Exception;
        void received(Event event);
        void discard(Event event);
        void ended();
    }
    private final Host host;
    private final long intervalMs;
    private volatile boolean stopped;
    private Thread thread;
    private long cursor;
    public NativeVoiceEvents(Host host,long cursor){this(host,cursor,1000);}
    public NativeVoiceEvents(Host host,long cursor,long intervalMs){this.host=host;this.cursor=cursor;this.intervalMs=intervalMs;}
    public synchronized void start(){
        if(thread!=null)return;
        thread=new Thread(this::run,"Anima-native-voice-events");thread.setDaemon(true);thread.start();
    }
    public synchronized void stop(){stopped=true;if(thread!=null)thread.interrupt();}
    private void run(){
        while(!stopped){
            try{
                int expected=host.outputEpoch();
                Batch batch=host.poll(cursor);
                if(stopped)return;
                if(!batch.voice){host.ended();return;}
                for(Event event:batch.events){
                    if(stopped)return;
                    if(event.sequence<=cursor)continue;
                    boolean played;
                    do{
                        while(!stopped&&expected==host.outputEpoch()&&host.inputPending())Thread.sleep(10);
                        if(stopped)return;
                        played=host.play(event,expected);
                        // Capture can begin between the readiness check and
                        // play. Hold that event rather than cancelling input.
                    }while(!played&&!stopped&&expected==host.outputEpoch()&&host.inputPending());
                    if(played){host.received(event);expected=host.outputEpoch();}
                    else host.discard(event);
                    cursor=Math.max(cursor,event.sequence);
                }
                cursor=Math.max(cursor,batch.cursor);
            }catch(Exception ignored){
                // Preserve the cursor and retry after transient LAN failures.
                // The server owns decision timing; this thread only delivers.
            }
            if(stopped)return;
            try{Thread.sleep(intervalMs);}catch(InterruptedException done){return;}
        }
    }
}
