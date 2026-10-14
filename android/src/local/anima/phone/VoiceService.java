package local.anima.phone;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
import android.media.AudioManager;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;

/** Explicit, user-started microphone/media lifetime, including screen-off use. */
public final class VoiceService extends Service {
    public static final String START="local.anima.phone.START_VOICE",STOP="local.anima.phone.STOP_VOICE",INTERRUPT="local.anima.phone.INTERRUPT_VOICE";
    private static final String CHANNEL="anima_live_voice";
    private static final int NOTICE=41;
    private PhoneRuntime runtime;
    private PowerManager.WakeLock wake;
    private WifiManager.WifiLock wifi;
    private AudioManager audio;
    private AudioFocusRequest focus;
    private boolean active=false;
    private final Handler main=new Handler(Looper.getMainLooper());
    private final Runnable heartbeat=new Runnable(){public void run(){
        if(!active)return;
        // A bounded lock is refreshed only by a live foreground session.
        if(wake!=null)wake.acquire(120000);
        if(runtime!=null)runtime.pingVisit();main.postDelayed(this,20000);
    }};
    @Override public IBinder onBind(Intent intent){return null;}
    @Override public int onStartCommand(Intent intent,int flags,int startId){
        runtime=((PhoneApplication)getApplication()).existingSession();
        String action=intent==null?"":intent.getAction();
        if(STOP.equals(action)){if(runtime!=null)runtime.stopVoiceFromSystem();stopSelf();return START_NOT_STICKY;}
        if(INTERRUPT.equals(action)){if(runtime!=null)runtime.interruptFromSystem();if(!active)stopSelf();return START_NOT_STICKY;}
        if(!START.equals(action)||runtime==null){stopSelf();return START_NOT_STICKY;}
        if(active)return START_NOT_STICKY;
        try{
            if(checkSelfPermission(Manifest.permission.RECORD_AUDIO)!=PackageManager.PERMISSION_GRANTED)throw new SecurityException("Microphone permission is required.");
            NotificationManager notices=(NotificationManager)getSystemService(NOTIFICATION_SERVICE);
            NotificationChannel channel=new NotificationChannel(CHANNEL,"Anima live voice",NotificationManager.IMPORTANCE_LOW);
            channel.setDescription("Controls for a voice visit that can continue with the screen off.");channel.setSound(null,null);channel.enableVibration(false);notices.createNotificationChannel(channel);
            int immutable=PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE;
            PendingIntent open=PendingIntent.getActivity(this,0,new Intent(this,MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),immutable);
            PendingIntent stop=PendingIntent.getService(this,1,new Intent(this,VoiceService.class).setAction(STOP),immutable);
            PendingIntent interrupt=PendingIntent.getService(this,2,new Intent(this,VoiceService.class).setAction(INTERRUPT),immutable);
            Notification notice=new Notification.Builder(this,CHANNEL).setSmallIcon(R.drawable.app_icon)
                .setContentTitle("Anima voice is active").setContentText("Microphone on · Tap to return to your visit")
                .setContentIntent(open).setOngoing(true).setOnlyAlertOnce(true).setShowWhen(false)
                .setCategory(Notification.CATEGORY_SERVICE).setVisibility(Notification.VISIBILITY_PRIVATE)
                .addAction(new Notification.Action.Builder(null,"Interrupt",interrupt).build())
                .addAction(new Notification.Action.Builder(null,"Stop voice",stop).build()).build();
            if(Build.VERSION.SDK_INT>=30)startForeground(NOTICE,notice,ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE|ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK);
            else startForeground(NOTICE,notice);
            audio=(AudioManager)getSystemService(AUDIO_SERVICE);
            focus=new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)
                .setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                .setOnAudioFocusChangeListener(change->{if(change==AudioManager.AUDIOFOCUS_LOSS||change==AudioManager.AUDIOFOCUS_LOSS_TRANSIENT){if(runtime!=null)runtime.stopVoiceFromSystem();}},main).build();
            if(audio.requestAudioFocus(focus)!=AudioManager.AUDIOFOCUS_REQUEST_GRANTED)throw new IllegalStateException("Another app is using audio. Try starting voice again when it finishes.");
            wake=((PowerManager)getSystemService(POWER_SERVICE)).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK,"Anima:LiveVoice");wake.setReferenceCounted(false);
            WifiManager manager=(WifiManager)getApplicationContext().getSystemService(WIFI_SERVICE);
            if(manager!=null){wifi=manager.createWifiLock(Build.VERSION.SDK_INT>=29?WifiManager.WIFI_MODE_FULL_LOW_LATENCY:WifiManager.WIFI_MODE_FULL_HIGH_PERF,"Anima:VoiceWiFi");wifi.setReferenceCounted(false);wifi.acquire();}
            active=true;main.post(heartbeat);runtime.serviceReady();
        }catch(Exception e){if(runtime!=null)runtime.serviceFailed(e);stopSelf();}
        // A killed process must not silently resume recording.
        return START_NOT_STICKY;
    }
    @Override public void onTaskRemoved(Intent root){if(runtime!=null)runtime.stopVoiceFromSystem();stopSelf();}
    @Override public void onDestroy(){
        active=false;main.removeCallbacks(heartbeat);
        if(runtime!=null)runtime.serviceStopped();
        if(focus!=null&&audio!=null)audio.abandonAudioFocusRequest(focus);
        if(wifi!=null&&wifi.isHeld())wifi.release();if(wake!=null&&wake.isHeld())wake.release();
        stopForeground(STOP_FOREGROUND_REMOVE);super.onDestroy();
    }
}
