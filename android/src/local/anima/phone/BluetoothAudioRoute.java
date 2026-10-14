package local.anima.phone;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.media.AudioDeviceInfo;
import android.media.AudioManager;
import android.os.Build;
import android.os.Handler;
import android.os.SystemClock;

/** Owns a Bluetooth communication route only for the lifetime of microphone capture.
 * All mutations run on the main thread; outputDevice is also read by playback workers. */
public final class BluetoothAudioRoute {
    public static final int AUTOMATIC = -1;
    private final Context context;
    private final AudioManager audio;
    private final Handler handler;
    private volatile AudioDeviceInfo outputDevice;
    private boolean ownsMode, ownsSelection, ownsSco, scoConnected;
    private int previousMode, generation;
    private BroadcastReceiver scoReceiver;
    private Runnable pending;
    private Callback callback;

    public interface Callback { void ready(); void failed(Exception error); }
    public BluetoothAudioRoute(Context context, AudioManager audio, Handler handler) {
        this.context=context;this.audio=audio;this.handler=handler;
    }
    public static boolean isBluetooth(AudioDeviceInfo device) {
        return device!=null && (device.getType()==AudioDeviceInfo.TYPE_BLUETOOTH_SCO ||
            device.getType()==AudioDeviceInfo.TYPE_BLE_HEADSET);
    }
    // Input and communication output have different IDs, but share type/address.
    public static boolean sameEndpoint(AudioDeviceInfo a, AudioDeviceInfo b) {
        if(a==null || b==null || a.getType()!=b.getType())return false;
        if(a.getId()==b.getId())return true;
        String address=a.getAddress();
        return !address.isEmpty() && address.equals(b.getAddress());
    }
    public AudioDeviceInfo output() { return outputDevice; }
    public boolean matchesInput(AudioDeviceInfo actual) {
        return sameEndpoint(outputDevice,actual);
    }
    private AudioDeviceInfo choose(AudioDeviceInfo requested) {
        if(Build.VERSION.SDK_INT>=31) {
            AudioDeviceInfo classic=null;
            for(AudioDeviceInfo device:audio.getAvailableCommunicationDevices()) {
                if(!isBluetooth(device))continue;
                if(requested!=null) { if(sameEndpoint(requested,device))return device; }
                else if(device.getType()==AudioDeviceInfo.TYPE_BLE_HEADSET)return device;
                else if(classic==null)classic=device;
            }
            return requested==null?classic:null;
        }
        for(AudioDeviceInfo device:audio.getDevices(AudioManager.GET_DEVICES_OUTPUTS))
            if(device.getType()==AudioDeviceInfo.TYPE_BLUETOOTH_SCO &&
                (requested==null || sameEndpoint(requested,device)))return device;
        return null;
    }
    public void start(AudioDeviceInfo requested, Callback completion) {
        stop();callback=completion;
        try {
            outputDevice=choose(requested);
            if(outputDevice==null)throw new Exception("No Bluetooth microphone is available. Enable calls/LE Audio for your headset in Android Bluetooth settings, or choose the phone microphone. Some headphones support playback only.");
            previousMode=audio.getMode();
            if(previousMode==AudioManager.MODE_IN_CALL)throw new Exception("End the phone call before using the Bluetooth microphone.");
            ownsMode=previousMode!=AudioManager.MODE_IN_COMMUNICATION;
            if(ownsMode)audio.setMode(AudioManager.MODE_IN_COMMUNICATION);
            if(Build.VERSION.SDK_INT>=31) {
                ownsSelection=true;
                if(!audio.setCommunicationDevice(outputDevice))throw new Exception("Android declined the Bluetooth microphone route. Choose another microphone or reconnect the headset.");
            } else {
                scoReceiver=new BroadcastReceiver() {
                    @Override public void onReceive(Context c, Intent intent) {
                        int state=intent.getIntExtra(AudioManager.EXTRA_SCO_AUDIO_STATE,-1);
                        scoConnected=state==AudioManager.SCO_AUDIO_STATE_CONNECTED;
                    }
                };
                Intent sticky=context.registerReceiver(scoReceiver,new IntentFilter(AudioManager.ACTION_SCO_AUDIO_STATE_UPDATED));
                if(sticky!=null)scoConnected=sticky.getIntExtra(AudioManager.EXTRA_SCO_AUDIO_STATE,-1)==AudioManager.SCO_AUDIO_STATE_CONNECTED;
                ownsSco=true;audio.startBluetoothSco();
                audio.setBluetoothScoOn(true);
            }
            final int epoch=generation;
            final long deadline=SystemClock.elapsedRealtime()+30000;
            pending=new Runnable() { public void run() {
                if(epoch!=generation || callback==null)return;
                try {
                    boolean connected=Build.VERSION.SDK_INT>=31?
                        sameEndpoint(outputDevice,audio.getCommunicationDevice()):scoConnected;
                    if(connected) {
                        Callback done=callback;callback=null;pending=null;done.ready();
                    } else if(SystemClock.elapsedRealtime()>=deadline) {
                        fail(new Exception("Bluetooth microphone routing timed out. Reconnect the headset or choose the phone microphone."));
                    } else handler.postDelayed(this,100);
                } catch(Exception error) { fail(error); }
            }};
            handler.post(pending);
        } catch(Exception error) { fail(error); }
    }
    private void fail(Exception error) {
        Callback done=callback;callback=null;stop();if(done!=null)done.failed(error);
    }
    public void stop() {
        generation++;
        if(pending!=null)handler.removeCallbacks(pending);pending=null;callback=null;
        if(scoReceiver!=null) { try {context.unregisterReceiver(scoReceiver);}catch(Exception ignored){} scoReceiver=null; }
        if(ownsSelection && Build.VERSION.SDK_INT>=31) { try {audio.clearCommunicationDevice();}catch(Exception ignored){} }
        if(ownsSco) { try {audio.stopBluetoothSco();audio.setBluetoothScoOn(false);}catch(Exception ignored){} }
        if(ownsMode) { try {if(audio.getMode()==AudioManager.MODE_IN_COMMUNICATION)audio.setMode(previousMode);}catch(Exception ignored){} }
        ownsMode=false;ownsSelection=false;ownsSco=false;scoConnected=false;outputDevice=null;
    }
}
