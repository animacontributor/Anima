package local.anima.phone;

import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioTrack;
import android.media.AudioDeviceInfo;

/** Keep stereo media for LE Audio; use the selected communication path for HFP. */
public final class AndroidVoiceSink implements NativeVoicePlayback.Sink {
    private final AudioTrack track;
    private volatile boolean stopped;
    public AndroidVoiceSink(int rate,int channels,AudioDeviceInfo bluetooth) throws Exception {
        boolean handsFree=bluetooth!=null && bluetooth.getType()==AudioDeviceInfo.TYPE_BLUETOOTH_SCO;
        int mask=channels==2?AudioFormat.CHANNEL_OUT_STEREO:AudioFormat.CHANNEL_OUT_MONO;
        int minimum=AudioTrack.getMinBufferSize(rate,mask,AudioFormat.ENCODING_PCM_FLOAT);
        if(minimum<=0)throw new Exception("Audio output format is unavailable.");
        track=new AudioTrack.Builder()
            .setAudioAttributes(new AudioAttributes.Builder().setUsage(handsFree?AudioAttributes.USAGE_VOICE_COMMUNICATION:AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
            .setAudioFormat(new AudioFormat.Builder().setSampleRate(rate).setChannelMask(mask).setEncoding(AudioFormat.ENCODING_PCM_FLOAT).build())
            .setTransferMode(AudioTrack.MODE_STREAM).setBufferSizeInBytes(Math.max(minimum,rate*channels*4/2)).build();
        if(track.getState()!=AudioTrack.STATE_INITIALIZED){track.release();throw new Exception("Could not initialize audio output.");}
        if(bluetooth!=null && !track.setPreferredDevice(bluetooth)){
            track.release();throw new Exception("Android declined headset playback. Reconnect the Bluetooth headset.");
        }
        track.play();
    }
    public void write(float[] samples,float volume) throws Exception {
        track.setVolume(Math.max(0,Math.min(1,volume)));
        for(int at=0;at<samples.length&&!stopped;){
            int n=track.write(samples,at,samples.length-at,AudioTrack.WRITE_BLOCKING);
            if(n<=0){if(stopped)return;throw new Exception("Android audio output stopped accepting samples.");}at+=n;
        }
    }
    public long played(){return Integer.toUnsignedLong(track.getPlaybackHeadPosition());}
    public void stop(){stopped=true;try{track.pause();track.flush();}catch(IllegalStateException ignored){}}
    public void close(){track.release();}
}
