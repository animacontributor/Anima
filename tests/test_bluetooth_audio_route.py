"""Exercise the production route controller on the JVM with a deterministic Android fake.

The fake tests route acceptance, asynchronous confirmation and cleanup, not Bluetooth
radio behavior. Real headset/phone testing is still required for audio quality.
"""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
STUBS = {
    "android/os/Build.java": """package android.os;
public class Build { public static class VERSION { public static int SDK_INT=35; } }""",
    "android/os/SystemClock.java": """package android.os;
public class SystemClock { public static long now; public static long elapsedRealtime(){return now;} }""",
    "android/os/Handler.java": """package android.os;
import java.util.*;
public class Handler {
 public final List<Runnable> queued=new ArrayList<>();
 public boolean post(Runnable r){queued.add(r);return true;}
 public boolean postDelayed(Runnable r,long delay){return post(r);}
 public void removeCallbacks(Runnable r){queued.removeIf(task->task==r);}
 public void tick(long millis){SystemClock.now+=millis;List<Runnable> tasks=new ArrayList<>(queued);queued.clear();for(Runnable r:tasks)r.run();}
}""",
    "android/content/BroadcastReceiver.java": """package android.content;
public abstract class BroadcastReceiver { public abstract void onReceive(Context c,Intent i); }""",
    "android/content/Intent.java": """package android.content;
public class Intent { public int state; public Intent(int state){this.state=state;} public int getIntExtra(String key,int fallback){return state;} }""",
    "android/content/IntentFilter.java": """package android.content;
public class IntentFilter { public IntentFilter(String action){} }""",
    "android/content/Context.java": """package android.content;
public class Context {
 public BroadcastReceiver receiver;public int unregistered;
 public Intent registerReceiver(BroadcastReceiver r,IntentFilter f){receiver=r;return null;}
 public void unregisterReceiver(BroadcastReceiver r){if(receiver!=r)throw new AssertionError();receiver=null;unregistered++;}
 public void state(int s){receiver.onReceive(this,new Intent(s));}
}""",
    "android/media/AudioDeviceInfo.java": """package android.media;
public class AudioDeviceInfo {
 public static final int TYPE_BLUETOOTH_SCO=7,TYPE_BLE_HEADSET=26;
 public final int id,type;public final String address;
 public AudioDeviceInfo(int id,int type,String address){this.id=id;this.type=type;this.address=address;}
 public int getId(){return id;}public int getType(){return type;}public String getAddress(){return address;}
}""",
    "android/media/AudioManager.java": """package android.media;
import java.util.*;
public class AudioManager {
 public static final int GET_DEVICES_OUTPUTS=2,MODE_IN_CALL=2,MODE_IN_COMMUNICATION=3,SCO_AUDIO_STATE_CONNECTED=1;
 public static final String EXTRA_SCO_AUDIO_STATE="state",ACTION_SCO_AUDIO_STATE_UPDATED="sco";
 public List<AudioDeviceInfo> devices=new ArrayList<>();
 public AudioDeviceInfo selected,current;public int mode,requests,clears,starts,stops;public boolean accept=true,sco;
 public List<AudioDeviceInfo> getAvailableCommunicationDevices(){return devices;}
 public AudioDeviceInfo[] getDevices(int flags){return devices.toArray(new AudioDeviceInfo[0]);}
 public int getMode(){return mode;}public void setMode(int v){mode=v;}
 public boolean setCommunicationDevice(AudioDeviceInfo d){requests++;selected=d;return accept;}
 public AudioDeviceInfo getCommunicationDevice(){return current;}
 public void clearCommunicationDevice(){clears++;selected=null;current=null;}
 public void startBluetoothSco(){starts++;}public void stopBluetoothSco(){stops++;}
 public void setBluetoothScoOn(boolean value){sco=value;}
}""",
}

CONTRACT = """package local.anima.phone;
import android.content.*;
import android.media.*;
import android.os.*;
public class BluetoothRouteContract {
 static void check(boolean value,String message){if(!value)throw new AssertionError(message);}
 static AudioDeviceInfo classic=new AudioDeviceInfo(10,7,"classic"),le=new AudioDeviceInfo(20,26,"le");
 static class Completion implements BluetoothAudioRoute.Callback {
  int ready,failed;Exception error;
  public void ready(){ready++;}public void failed(Exception e){failed++;error=e;}
 }
 static class Fixture {
  Context context=new Context();AudioManager audio=new AudioManager();Handler handler=new Handler();
  BluetoothAudioRoute route=new BluetoothAudioRoute(context,audio,handler);Completion done=new Completion();
  Fixture(){Build.VERSION.SDK_INT=35;SystemClock.now=0;audio.devices.add(classic);audio.devices.add(le);}
 }
 public static void main(String[] args){
  Fixture f=new Fixture();f.route.start(null,f.done);
  check(f.audio.selected==le,"Automatic selection must prefer LE Audio");
  f.handler.tick(100);check(f.done.ready==0,"A request is not confirmation");
  f.audio.current=le;f.handler.tick(100);check(f.done.ready==1,"Confirmed LE route must complete");
  check(f.route.matchesInput(new AudioDeviceInfo(21,26,"le")),"Input/output IDs differ");
  check(!f.route.matchesInput(new AudioDeviceInfo(22,26,"other")),"Reject another headset");
  check(!f.route.matchesInput(new AudioDeviceInfo(23,15,"")),"Reject the phone microphone");
  f.route.stop();check(f.audio.clears==1&&f.audio.mode==0&&f.route.output()==null,"Release route and restore mode");
  f.route.stop();check(f.audio.clears==1,"Cleanup is idempotent");

  f=new Fixture();f.route.start(new AudioDeviceInfo(11,7,"classic"),f.done);
  check(f.audio.selected==classic,"Respect explicit classic headset despite LE availability");
  f.audio.current=classic;f.handler.tick(100);check(f.done.ready==1,"Classic route confirmation");f.route.stop();

  f=new Fixture();f.audio.accept=false;f.route.start(null,f.done);
  check(f.done.failed==1&&f.done.ready==0&&f.audio.mode==0&&f.audio.clears==1,"Rejected route cleans up");

  f=new Fixture();f.route.start(null,f.done);f.audio.current=classic;f.handler.tick(30000);
  check(f.done.failed==1&&f.done.ready==0&&f.audio.mode==0&&f.handler.queued.isEmpty(),"Wrong route times out and cleans up");

  f=new Fixture();f.route.start(null,f.done);f.route.stop();f.audio.current=le;f.handler.tick(30000);
  check(f.done.ready==0&&f.done.failed==0,"Cancelled routing cannot resurrect capture");

  f=new Fixture();f.audio.devices.clear();f.audio.devices.add(new AudioDeviceInfo(40,8,"media-only"));f.route.start(null,f.done);
  check(f.done.failed==1&&f.audio.requests==0&&f.audio.mode==0,"Playback-only headphones are not microphones");

  f=new Fixture();f.audio.mode=AudioManager.MODE_IN_CALL;f.route.start(null,f.done);
  check(f.done.failed==1&&f.audio.mode==AudioManager.MODE_IN_CALL&&f.audio.requests==0,"Do not interrupt a phone call");

  f=new Fixture();f.route.start(null,f.done);f.audio.mode=AudioManager.MODE_IN_CALL;f.route.stop();
  check(f.audio.mode==AudioManager.MODE_IN_CALL,"Do not overwrite a mode change from another app");

  f=new Fixture();Build.VERSION.SDK_INT=30;f.audio.devices.remove(le);f.route.start(null,f.done);
  f.handler.tick(100);check(f.done.ready==0&&f.audio.starts==1,"Legacy SCO waits for connection broadcast");
  f.context.state(AudioManager.SCO_AUDIO_STATE_CONNECTED);f.handler.tick(100);
  check(f.done.ready==1&&f.audio.sco,"Legacy SCO confirmation");f.route.stop();
  check(f.audio.stops==1&&!f.audio.sco&&f.context.unregistered==1&&f.audio.mode==0,"Legacy SCO cleanup");

  f=new Fixture();Build.VERSION.SDK_INT=26;f.audio.devices.remove(le);f.route.start(null,f.done);f.handler.tick(30000);
  check(f.done.failed==1&&f.audio.stops==1&&f.context.receiver==null&&f.audio.mode==0,"Legacy timeout cleanup");
  System.out.println("PASS: Bluetooth LE/HFP selection, confirmation, rejection, timeout, cancellation, endpoint verification and cleanup");
 }
}
"""


class BluetoothRouteTests(unittest.TestCase):
    def test_native_routing_contract(self):
        jdk = Path(os.environ.get("ANIMA_ANDROID_JDK", ROOT / ".runtime/android-build/jdk"))
        suffix = ".exe" if os.name == "nt" else ""
        javac = jdk / "bin" / ("javac" + suffix)
        if not javac.exists():
            self.skipTest("Private Android build JDK not installed")
        with tempfile.TemporaryDirectory(prefix="bluetooth-route-") as directory:
            temporary = Path(directory)
            sources = []
            for name, source in {**STUBS, "local/anima/phone/BluetoothRouteContract.java": CONTRACT}.items():
                path = temporary / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(source, encoding="utf-8")
                sources.append(str(path))
            sources.append(str(ROOT / "android/src/local/anima/phone/BluetoothAudioRoute.java"))
            classes = temporary / "classes"
            classes.mkdir()
            subprocess.run([str(javac), "-d", str(classes), *sources], check=True)
            subprocess.run([str(jdk / "bin" / ("java" + suffix)), "-cp", str(classes),
                            "local.anima.phone.BluetoothRouteContract"], check=True)


if __name__ == "__main__":
    unittest.main()
