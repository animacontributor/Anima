package local.anima.phone;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.Context;
import android.content.MutableContextWrapper;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioDeviceInfo;
import android.media.AudioDeviceCallback;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.media.audiofx.AcousticEchoCanceler;
import android.media.audiofx.NoiseSuppressor;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.OpenableColumns;
import android.util.Base64;
import android.view.WindowInsets;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.webkit.WebSettings;
import android.webkit.RenderProcessGoneDetail;
import android.widget.FrameLayout;
import org.json.JSONObject;
import org.json.JSONArray;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.URI;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** The WebView loads bundled assets only. HTTP and microphone access are native. */
public final class PhoneRuntime {
    private static final String HOME = "https://appassets.androidplatform.net/index.html";
    private static final int MIC_PERMISSION = 31, PICK_FILES = 32;
    private static final int MAX_FILE = 50 * 1024 * 1024;
    private WebView web;
    private final Context context;
    private final MutableContextWrapper webContext;
    private final Handler main = new Handler(Looper.getMainLooper());
    private Activity activity;
    private String voiceVisit="", serviceMicId="";
    private volatile boolean backgroundVoice=false;
    private volatile int voiceServerVersion=-1;
    private long voiceEventCursor;
    private NativeVoiceEvents voiceEvents;
    private volatile NativeVoiceCapture voiceCapture;
    private final ExecutorService voiceInput=Executors.newSingleThreadExecutor();
    private final java.util.concurrent.Semaphore inputSlots=new java.util.concurrent.Semaphore(2);
    private volatile int inputEpoch;
    private volatile boolean capturePaused;
    private final java.util.concurrent.atomic.AtomicBoolean pingOutstanding=new java.util.concurrent.atomic.AtomicBoolean();
    private SharedPreferences prefs;
    private final ExecutorService network = Executors.newFixedThreadPool(4);
    private final NativeVoicePlayback playback;
    private volatile float outputVolume=1;
    private volatile boolean speakerOutput=true;
    private volatile long outputSuppressUntil;
    private volatile boolean recording = false;
    private volatile int micEpoch = 0;
    private volatile long microphoneFrames = 0;
    private volatile long lastMicrophoneFrame;
    private AudioRecord recorder;
    private AcousticEchoCanceler echo;
    private NoiseSuppressor noiseSuppressor;
    private AudioManager audioManager;
    private AudioDeviceCallback deviceCallback;
    private final BluetoothAudioRoute bluetoothRoute;
    private String startingMicId="";
    private volatile AudioDeviceInfo requestedInput;
    private volatile int selectedInputId;
    private volatile boolean microphoneVerified=false;
    private Runnable verifyMicrophone;
    private String uploadVisit = "";
    private String pendingMicId = "";
    private boolean destroyed = false;
    private boolean requestingMicPermission = false;
    private boolean foregroundVisible = false, pendingPermissionStart = false;

    public PhoneRuntime(Context appContext) {
        context=appContext.getApplicationContext();webContext=new MutableContextWrapper(context);
        prefs = context.getSharedPreferences("connection", Context.MODE_PRIVATE);
        audioManager = (AudioManager)context.getSystemService(Context.AUDIO_SERVICE);
        bluetoothRoute=new BluetoothAudioRoute(context,audioManager,main);
        playback=new NativeVoicePlayback(new NativeVoicePlayback.Host(){
            public NativeVoicePlayback.Status status(String job,String visit,int next) throws Exception {
                JSONObject value=voiceRequest("/api/voice/status",job,visit,null);
                NativeVoicePlayback.Status state=new NativeVoicePlayback.Status();
                state.done=value.optBoolean("done");state.cancelled=value.optBoolean("cancelled");state.error=value.optString("error","");
                JSONArray chunks=value.optJSONArray("chunks");
                if(chunks!=null)for(int i=0;i<chunks.length();i++)if(chunks.getJSONObject(i).getInt("index")==next)state.available=true;
                return state;
            }
            public byte[] audio(String job,String visit,int index) throws Exception {
                String route="/api/audio/"+job+"/"+index+"?visit="+URLEncoder.encode(visit,"UTF-8");
                HttpURLConnection c=connection(validateAddress(prefs.getString("address","")),route,"GET",true);
                try{if(c.getResponseCode()!=200)throw new Exception("Speech audio became unavailable.");try(InputStream in=c.getInputStream()){return read(in,12000000);}}finally{c.disconnect();}
            }
            public void acknowledge(String job,String visit,int index) throws Exception {
                JSONObject extra=new JSONObject();extra.put("index",index);voiceRequest("/api/voice/ack",job,visit,extra);
            }
            public void delivered(String job,String visit,int completed,boolean partial,String reason) throws Exception {
                JSONObject extra=new JSONObject();extra.put("completed",completed);extra.put("partial",partial);extra.put("reason",reason);
                voiceRequest("/api/voice/delivery",job,visit,extra);
            }
            public float volume(){return outputVolume;}
        },(rate,channels)->new AndroidVoiceSink(rate,channels,bluetoothRoute.output()));
        deviceCallback = new AudioDeviceCallback() {
            @Override public void onAudioDevicesAdded(AudioDeviceInfo[] devices) { js("window.audioDevicesChanged && window.audioDevicesChanged();"); }
            @Override public void onAudioDevicesRemoved(AudioDeviceInfo[] devices) { js("window.audioDevicesChanged && window.audioDevicesChanged();"); }
        };
        audioManager.registerAudioDeviceCallback(deviceCallback, new Handler(Looper.getMainLooper()));
        web = new WebView(webContext);
        WebView.setWebContentsDebuggingEnabled((context.getApplicationInfo().flags & android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE) != 0);
        web.setRendererPriorityPolicy(WebView.RENDERER_PRIORITY_IMPORTANT,false);
        web.setBackgroundColor(0xff171512);
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false); settings.setAllowContentAccess(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setMediaPlaybackRequiresUserGesture(false);
        web.addJavascriptInterface(new Bridge(), "AnimaNative");
        web.setWebViewClient(new WebViewClient() {
            @Override public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if ("blob".equals(uri.getScheme()) || "data".equals(uri.getScheme())) return null;
                if (!"https".equals(uri.getScheme()) || !"appassets.androidplatform.net".equals(uri.getHost()))
                    return new WebResourceResponse("text/plain", "UTF-8", new java.io.ByteArrayInputStream(new byte[0]));
                String name = uri.getPath().substring(1);
                if (name.isEmpty()) name = "index.html";
                if (!name.matches("[a-zA-Z0-9_./-]+") || name.contains(".."))
                    return new WebResourceResponse("text/plain", "UTF-8", new java.io.ByteArrayInputStream(new byte[0]));
                try {
                    String mime = name.endsWith(".js") ? "text/javascript" : name.endsWith(".css") ? "text/css" :
                                  name.endsWith(".svg") ? "image/svg+xml" : "text/html";
                    return new WebResourceResponse(mime, "UTF-8", context.getAssets().open(name));
                } catch (Exception e) {
                    return new WebResourceResponse("text/plain", "UTF-8", new java.io.ByteArrayInputStream(new byte[0]));
                }
            }
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) { return true; }
            @Override public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
                stopVoiceFromSystem();close();return true;
            }
        });
        web.loadUrl(HOME);
    }
    public WebView view(){return web;}
    private void runOnUiThread(Runnable task){if(Looper.myLooper()==Looper.getMainLooper())task.run();else main.post(task);}
    public void attach(Activity front){activity=front;webContext.setBaseContext(front);}
    public void detach(Activity front){if(activity==front){activity=null;foregroundVisible=false;webContext.setBaseContext(context);if(!backgroundVoice)close();}}
    public boolean alive(){return !destroyed;}
    public boolean backgroundVoice(){return backgroundVoice;}

    private void js(String script) {
        runOnUiThread(() -> { if (!destroyed && web != null) web.evaluateJavascript(script, null); });
    }
    private void reply(String id, JSONObject value) {
        js("window.nativeReply(" + JSONObject.quote(id) + "," + value.toString() + ");");
    }
    private JSONObject error(Exception e) {
        JSONObject out = new JSONObject();
        try {
            String message = e.getMessage();
            if (e instanceof java.net.SocketTimeoutException) message = "The computer took too long to answer. Check its phone server.";
            if (e instanceof java.net.ConnectException) message = "Cannot reach the computer. Check Wi-Fi, address and its phone server.";
            out.put("error", message == null ? "Connection failed." : message);
        } catch (Exception ignored) {}
        return out;
    }
    private String validateAddress(String text) throws Exception {
        URI uri = new URI(text.trim());
        if (!"http".equalsIgnoreCase(uri.getScheme()) || uri.getHost() == null || uri.getUserInfo() != null ||
            uri.getQuery() != null || uri.getFragment() != null || !(uri.getPath().isEmpty() || uri.getPath().equals("/")))
            throw new Exception("Enter a local address like http://192.168.1.20:8780");
        InetAddress address = InetAddress.getByName(uri.getHost());
        if (!(address.isSiteLocalAddress() || address.isLoopbackAddress() || address.isLinkLocalAddress()))
            throw new Exception("This version connects to your local Wi-Fi computer only.");
        int port = uri.getPort();
        if (port < 1 || port > 65535) throw new Exception("Include the phone server port, usually :8780.");
        return "http://" + uri.getHost() + ":" + port;
    }
    private HttpURLConnection connection(String base, String route, String method, boolean authenticate) throws Exception {
        if (!route.startsWith("/api/") || route.contains("..") || route.indexOf('\n') >= 0 || route.indexOf('\r') >= 0)
            throw new Exception("Invalid app endpoint.");
        HttpURLConnection c = (HttpURLConnection) new URL(base + route).openConnection();
        c.setInstanceFollowRedirects(false);
        c.setConnectTimeout(10000);
        c.setReadTimeout(route.equals("/api/send") || route.equals("/api/voice/turn") || route.equals("/api/voice/recognize") || route.equals("/api/voice/initiative") || route.equals("/api/voice/start") ? 720000 : route.equals("/api/upload") ? 120000 : 15000);
        c.setRequestMethod(method);
        if (authenticate) c.setRequestProperty("Authorization", "Bearer " + prefs.getString("token", ""));
        return c;
    }
    private byte[] read(InputStream input, int limit) throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] block = new byte[16384]; int n;
        while ((n = input.read(block)) != -1) {
            if (out.size() + n > limit) throw new Exception("Response is too large.");
            out.write(block, 0, n);
        }
        return out.toByteArray();
    }
    private JSONObject jsonRequest(String base, String route, String method, JSONObject body, boolean auth) throws Exception {
        HttpURLConnection c = connection(base, route, method, auth);
        try {
            if (body != null) {
                byte[] data = body.toString().getBytes(StandardCharsets.UTF_8);
                c.setDoOutput(true); c.setRequestProperty("Content-Type", "application/json");
                c.setFixedLengthStreamingMode(data.length);
                try (OutputStream out = c.getOutputStream()) { out.write(data); }
            }
            int status = c.getResponseCode();
            InputStream input = status >= 400 ? c.getErrorStream() : c.getInputStream();
            if (input == null) throw new Exception("Computer returned HTTP " + status);
            try (InputStream in = input) {
                JSONObject result = new JSONObject(new String(read(in, 8000000), StandardCharsets.UTF_8));
                result.put("_status", status); return result;
            }
        } finally { c.disconnect(); }
    }

    private JSONObject voiceRequest(String route,String job,String visit,JSONObject extra) throws Exception {
        JSONObject body=extra==null?new JSONObject():extra;body.put("job",job);body.put("visit",visit);
        JSONObject value=jsonRequest(validateAddress(prefs.getString("address","")),route,"POST",body,true);
        if(value.optInt("_status",200)>=400)throw new Exception(value.optString("error","Phone visit ended."));
        return value;
    }

    public final class Bridge {
        @JavascriptInterface public String configuration() {
            JSONObject value = new JSONObject();
            try { value.put("address", prefs.getString("address", "")); value.put("paired", !prefs.getString("token", "").isEmpty()); }
            catch (Exception ignored) {}
            return value.toString();
        }
        @JavascriptInterface public void pair(String id, String address, String code) {
            network.execute(() -> {
                try {
                    String base = validateAddress(address);
                    JSONObject data = new JSONObject(); data.put("code", code);
                    JSONObject result = jsonRequest(base, "/api/pair", "POST", data, false);
                    if (result.has("token")) {
                        prefs.edit().putString("address", base).putString("token", result.getString("token")).apply();
                        result.remove("token"); // The token is never exposed to web content.
                    }
                    reply(id, result);
                } catch (Exception e) { reply(id, error(e)); }
            });
        }
        @JavascriptInterface public void request(String id, String route, String method, String body) {
            if(route.equals("/api/voice/interrupt")){
                String job="";try{job=new JSONObject(body).optString("job");}catch(Exception ignored){}
                synchronized(playback){NativeVoicePlayback.Job state=playback.job(job);if(job.isEmpty()||state!=null&&!state.cancelled&&!state.done){inputEpoch++;playback.stop();}}
            }else if(route.equals("/api/voice/stop")||route.equals("/api/leave")){inputEpoch++;playback.stop();}
            final int outputEpoch=playback.epoch();
            network.execute(() -> {
                try {
                    String base = validateAddress(prefs.getString("address", ""));
                    if (!(method.equals("GET") || method.equals("POST") || method.equals("BINARY"))) throw new Exception("Invalid request method.");
                    if (method.equals("BINARY")) {
                        HttpURLConnection c = connection(base, route, "GET", true);
                        try {
                            int status = c.getResponseCode();
                            if (status >= 400) {
                                try (InputStream in = c.getErrorStream()) { reply(id, new JSONObject(new String(read(in, 100000), StandardCharsets.UTF_8))); }
                            } else {
                                JSONObject result = new JSONObject();
                                try (InputStream in = c.getInputStream()) { result.put("audio", Base64.encodeToString(read(in, 12000000), Base64.NO_WRAP)); }
                                result.put("mime", c.getContentType()); reply(id, result);
                            }
                        } finally { c.disconnect(); }
                    } else {
                        JSONObject data=body.isEmpty()?null:new JSONObject(body);
                        JSONObject result=jsonRequest(base,route,method,data,true);
                        if(route.equals("/api/voice/recognize")&&result.optBoolean("accepted",false)&&backgroundVoice&&data!=null&&voiceVisit.equals(data.optString("visit"))){
                            synchronized(playback){if(outputEpoch==playback.epoch())playback.stop();}
                        }
                        if((route.equals("/api/voice/turn")||route.equals("/api/voice/initiative")||route.equals("/api/send")&&data!=null&&data.optBoolean("spoken"))&&result.has("speech_job")&&!result.isNull("speech_job")){
                            result.put("native_playback",true);
                            if(backgroundVoice&&data!=null&&voiceVisit.equals(data.optString("visit")))playback.start(result.getString("speech_job"),voiceVisit,outputEpoch);
                        }
                        reply(id,result);
                    }
                } catch (Exception e) { reply(id, error(e)); }
            });
        }
        @JavascriptInterface public void stopPlayback(){inputEpoch++;playback.stop();}
        @JavascriptInterface public void setCapturePaused(boolean value){capturePaused=value;if(!value)outputSuppressUntil=android.os.SystemClock.elapsedRealtime()+450;}
        @JavascriptInterface public void playbackStatus(String id,String job){
            JSONObject value=new JSONObject();NativeVoicePlayback.Job state=playback.job(job);
            try{value.put("cancelled",state==null||state.cancelled);value.put("done",state==null||state.done);value.put("started",state!=null&&state.started);value.put("completed",state==null?0:state.completed);if(state!=null&&!state.error.isEmpty())value.put("error",state.error);}catch(Exception ignored){}
            reply(id,value);
        }
        @JavascriptInterface public void forget() { stopMicrophone(); prefs.edit().remove("address").remove("token").apply(); }
        @JavascriptInterface public void chooseFiles(String visit) {
            runOnUiThread(() -> {
                uploadVisit = visit;
                Intent pick = new Intent(Intent.ACTION_OPEN_DOCUMENT); pick.setType("*/*");
                pick.addCategory(Intent.CATEGORY_OPENABLE); pick.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                if(activity!=null)activity.startActivityForResult(pick, PICK_FILES);
            });
        }
        @JavascriptInterface public void startMicrophone(String id) {
            runOnUiThread(() -> {
                if (context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                    if(activity==null||!foregroundVisible){reply(id,error(new Exception("Open the app to grant microphone permission.")));return;}
                    pendingMicId = id; requestingMicPermission = true;
                    activity.requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, MIC_PERMISSION);
                } else startVoiceMicrophone(id);
            });
        }
        @JavascriptInterface public void audioDevices(String id) { runOnUiThread(() -> reply(id, audioInfo())); }
        @JavascriptInterface public void configureAudio(String id, String options) {
            runOnUiThread(() -> {
                try {
                    JSONObject data = new JSONObject(options);
                    outputVolume=(float)Math.max(0,Math.min(1,data.optDouble("volume",100)/100));
                    speakerOutput="speaker".equals(data.optString("mode","speaker"));
                    if(voiceCapture!=null){voiceCapture.threshold=.004*Math.pow(8,(100-data.optDouble("sensitivity",60))/100);voiceCapture.silenceMs=data.optInt("silence",1000);}
                    prefs.edit().putInt("micInput", Math.max(BluetoothAudioRoute.AUTOMATIC,data.optInt("input",0)))
                        .putBoolean("micEcho",data.optBoolean("echo",true)).putBoolean("micNoise",data.optBoolean("noise",false)).apply();
                    reply(id,audioInfo());
                } catch (Exception e) { reply(id,error(e)); }
            });
        }
        @JavascriptInterface public void prepareVoice(String id,String visit,int version,long cursor,String options) {
            voiceServerVersion=version;voiceEventCursor=cursor;
            inputEpoch++;
            NativeVoiceCapture capture=new NativeVoiceCapture(new NativeVoiceCapture.Listener(){
                public void started(){notifyVoiceInput("checking",null);}
                public void utterance(byte[] wave){submitVoiceInput(wave,visit,version);}
            });
            try{JSONObject data=new JSONObject(options);capture.threshold=data.optDouble("threshold",.0092);capture.silenceMs=data.optInt("silence_ms",1000);
                capture.minSpeechMs=data.optInt("min_speech_ms",160);capture.interruptMs=data.optInt("interrupt_ms",350);capture.maxSeconds=30;
            }catch(Exception ignored){}
            voiceCapture=capture;capturePaused=false;
            runOnUiThread(()->{voiceVisit=visit;if(backgroundVoice)startVoiceEvents();JSONObject ok=new JSONObject();try{ok.put("ok",true);}catch(Exception ignored){}reply(id,ok);});
        }
        @JavascriptInterface public void stopVoiceService() { runOnUiThread(()->context.stopService(new Intent(context,VoiceService.class))); }
        @JavascriptInterface public void stopMicrophone() { runOnUiThread(() -> PhoneRuntime.this.stopMicrophone()); }
        @JavascriptInterface public void closeApp() { runOnUiThread(() -> {if(activity!=null)activity.finish();}); }
    }
    private void startVoiceMicrophone(String id){
        if(backgroundVoice){beginMicrophone(id);return;}
        if(!foregroundVisible||activity==null){reply(id,error(new Exception("Start voice while the app is open, then you can lock the screen.")));return;}
        if(voiceVisit.isEmpty()){reply(id,error(new Exception("Open a visit before starting voice.")));return;}
        serviceMicId=id;
        try{
            if(Build.VERSION.SDK_INT>=33 && !prefs.getBoolean("notificationsAsked",false) && context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)!=PackageManager.PERMISSION_GRANTED){
                prefs.edit().putBoolean("notificationsAsked",true).apply();pendingMicId=id;serviceMicId="";requestingMicPermission=true;
                activity.requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS},33);
                return;
            }
            context.startForegroundService(new Intent(context,VoiceService.class).setAction(VoiceService.START));
        }catch(Exception e){serviceMicId="";reply(id,error(e));}
    }
    public void serviceReady(){backgroundVoice=true;if(voiceServerVersion>=0)startVoiceEvents();web.onResume();web.resumeTimers();String id=serviceMicId;serviceMicId="";if(!id.isEmpty())beginMicrophone(id);}
    public void serviceFailed(Exception e){String id=serviceMicId;serviceMicId="";if(!id.isEmpty())reply(id,error(e));}
    public void serviceStopped(){inputEpoch++;backgroundVoice=false;stopVoiceEvents();playback.stop();stopMicrophone();sendVoiceControl("/api/voice/stop");js("window.backgroundVoiceStopped && window.backgroundVoiceStopped();");}
    public void interruptFromSystem(){inputEpoch++;playback.stop();sendVoiceControl("/api/voice/interrupt");js("window.phoneApp && window.phoneApp.voice.cancelReply();");}
    public void stopVoiceFromSystem(){inputEpoch++;backgroundVoice=false;stopVoiceEvents();playback.stop();stopMicrophone();sendVoiceControl("/api/voice/stop");js("window.backgroundVoiceStopped && window.backgroundVoiceStopped();");context.stopService(new Intent(context,VoiceService.class));}

    private boolean inputCurrent(String visit,int version,int epoch){return !destroyed&&backgroundVoice&&voiceVisit.equals(visit)&&voiceServerVersion==version&&inputEpoch==epoch;}
    private void notifyVoiceInput(String stage,JSONObject result){
        try{JSONObject event=new JSONObject();event.put("stage",stage);event.put("visit",voiceVisit);event.put("voice_version",voiceServerVersion);
            if(result!=null)event.put("result",result);js("window.nativeVoiceInput && window.nativeVoiceInput("+event.toString()+");");
        }catch(Exception ignored){}
    }
    private void submitVoiceInput(byte[] wave,final String visit,final int version){
        final int epoch=inputEpoch;
        if(!inputCurrent(visit,version,epoch)||!inputSlots.tryAcquire())return;
        voiceInput.execute(()->{try{
            if(!inputCurrent(visit,version,epoch))return;
            JSONObject body=new JSONObject();body.put("visit",visit);body.put("voice_version",version);body.put("audio",Base64.encodeToString(wave,Base64.NO_WRAP));
            JSONObject accepted=jsonRequest(validateAddress(prefs.getString("address","")),"/api/voice/recognize","POST",body,true);
            if(!inputCurrent(visit,version,epoch))return;
            if(!accepted.optBoolean("accepted")){notifyVoiceInput("rejected",accepted);return;}
            NativeVoicePlayback.Job old=playback.current();playback.stop();notifyVoiceInput("accepted",accepted);
            long deadline=android.os.SystemClock.elapsedRealtime()+15000;
            while(old!=null&&!old.receipted&&inputCurrent(visit,version,epoch)&&android.os.SystemClock.elapsedRealtime()<deadline)Thread.sleep(10);
            if(!inputCurrent(visit,version,epoch))return;
            JSONObject prompt=new JSONObject();prompt.put("visit",visit);prompt.put("voice_version",version);prompt.put("input",accepted.getString("input"));
            int outputEpoch=playback.epoch();
            JSONObject reply=jsonRequest(validateAddress(prefs.getString("address","")),"/api/voice/turn","POST",prompt,true);
            if(!inputCurrent(visit,version,epoch))return;
            reply.put("native_playback",true);
            if(reply.has("speech_job")&&!reply.isNull("speech_job"))playback.start(reply.getString("speech_job"),visit,outputEpoch);
            notifyVoiceInput("reply",reply);
        }catch(Exception error){try{JSONObject value=new JSONObject();value.put("error",error.getMessage());if(inputCurrent(visit,version,epoch))notifyVoiceInput("error",value);}catch(Exception ignored){}
        }finally{inputSlots.release();}});
    }

    private void stopVoiceEvents(){if(voiceEvents!=null){voiceEvents.stop();voiceEvents=null;}}
    private void sendVoiceControl(String route){
        final String visit=voiceVisit;final int version=voiceServerVersion;
        if(visit.isEmpty()||destroyed)return;
        PowerManager.WakeLock controlWake=((PowerManager)context.getSystemService(Context.POWER_SERVICE)).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK,"Anima:VoiceControl");
        controlWake.acquire(15000);
        Thread control=new Thread(()->{try{
            JSONObject body=new JSONObject();body.put("visit",visit);body.put("voice_version",version);
            jsonRequest(validateAddress(prefs.getString("address","")),route,"POST",body,true);
        }catch(Exception ignored){}finally{if(controlWake.isHeld())controlWake.release();}},"Anima-native-voice-control");
        control.setDaemon(true);control.start();
    }
    private void startVoiceEvents(){
        stopVoiceEvents();final String visit=voiceVisit;final int version=voiceServerVersion;
        voiceEvents=new NativeVoiceEvents(new NativeVoiceEvents.Host(){
            public int outputEpoch(){return playback.epoch();}
            public boolean inputPending(){return (voiceCapture!=null&&voiceCapture.pending())||inputSlots.availablePermits()<2;}
            public NativeVoiceEvents.Batch poll(long after) throws Exception {
                JSONObject body=new JSONObject();body.put("visit",visit);body.put("after",after);
                JSONObject state=new JSONObject();state.put("phase",playback.playing()?"speaking":"listening");
                state.put("mic_age_ms",Math.max(0,android.os.SystemClock.elapsedRealtime()-lastMicrophoneFrame));
                JSONArray blocked=new JSONArray();if(voiceCapture!=null&&voiceCapture.active())blocked.put("capture");
                if(inputSlots.availablePermits()<2)blocked.put("recognizing");if(!microphoneVerified)blocked.put("microphone_stale");
                state.put("blocked",blocked);body.put("state",state);
                JSONObject result=jsonRequest(validateAddress(prefs.getString("address","")),"/api/voice/events","POST",body,true);
                int status=result.optInt("_status",200);
                if(status>=400&&status!=401&&status!=409)throw new Exception(result.optString("error","Voice event polling failed."));
                NativeVoiceEvents.Batch batch=new NativeVoiceEvents.Batch();batch.cursor=result.optLong("cursor",after);
                batch.voice=backgroundVoice&&voiceVisit.equals(visit)&&voiceServerVersion==version
                    &&result.optBoolean("voice",false)&&result.optInt("voice_version",-1)==version&&result.optInt("_status",200)!=409;
                JSONArray events=result.optJSONArray("events");
                if(events!=null)for(int i=0;i<events.length();i++){
                    JSONObject item=events.getJSONObject(i);
                    batch.events.add(new NativeVoiceEvents.Event(item.getLong("sequence"),item.getString("speech_job"),item.optString("reply","")));
                }
                return batch;
            }
            public boolean play(NativeVoiceEvents.Event event,int expected){
                return backgroundVoice&&voiceVisit.equals(visit)&&voiceServerVersion==version&&!inputPending()&&playback.start(event.job,visit,expected,this::inputPending);
            }
            public void received(NativeVoiceEvents.Event event){
                try{
                    JSONObject value=new JSONObject();value.put("sequence",event.sequence);value.put("visit",visit);value.put("voice_version",version);
                    value.put("reply",event.reply);value.put("speech_job",event.job);value.put("native_playback",true);
                    js("window.nativeVoiceContinuation && window.nativeVoiceContinuation("+value.toString()+");");
                }catch(Exception ignored){}
            }
            public void discard(NativeVoiceEvents.Event event){
                try{JSONObject data=new JSONObject();data.put("completed",0);data.put("partial",false);data.put("reason","native continuation superseded");
                    voiceRequest("/api/voice/delivery",event.job,visit,data);voiceRequest("/api/voice/interrupt",event.job,visit,null);
                }catch(Exception ignored){}
            }
            public void ended(){main.post(()->{if(voiceVisit.equals(visit)&&voiceServerVersion==version)stopVoiceFromSystem();});}
        },voiceEventCursor);voiceEvents.start();
    }
    public void pingVisit(){
        if(!backgroundVoice||voiceVisit.isEmpty()||!pingOutstanding.compareAndSet(false,true))return;
        final String visit=voiceVisit;
        network.execute(()->{try{
            JSONObject body=new JSONObject();body.put("visit",visit);
            JSONObject result=jsonRequest(validateAddress(prefs.getString("address","")),"/api/ping","POST",body,true);
            if(result.optInt("_status",200)==409)main.post(()->stopVoiceFromSystem());
        }catch(Exception ignored){}finally{pingOutstanding.set(false);}});
    }

    private String deviceLabel(AudioDeviceInfo device) {
        String kind;
        switch (device.getType()) {
            case AudioDeviceInfo.TYPE_BUILTIN_MIC: kind="Phone microphone"; break;
            case AudioDeviceInfo.TYPE_WIRED_HEADSET: kind="Wired headset microphone"; break;
            case AudioDeviceInfo.TYPE_USB_DEVICE:
            case AudioDeviceInfo.TYPE_USB_HEADSET: kind="USB microphone"; break;
            case AudioDeviceInfo.TYPE_BLUETOOTH_SCO: kind="Bluetooth headset microphone"; break;
            case AudioDeviceInfo.TYPE_BLE_HEADSET: kind="Bluetooth LE microphone"; break;
            default: kind="Microphone";
        }
        return kind+" — "+device.getProductName();
    }
    private synchronized JSONObject audioInfo() {
        JSONObject info = new JSONObject(); JSONArray inputs = new JSONArray();
        try {
            JSONObject automatic=new JSONObject();automatic.put("id",BluetoothAudioRoute.AUTOMATIC);
            automatic.put("label","Bluetooth microphone (automatic, prefer LE Audio)");inputs.put(automatic);
            for (AudioDeviceInfo device : audioManager.getDevices(AudioManager.GET_DEVICES_INPUTS)) {
                JSONObject item=new JSONObject();item.put("id",device.getId());item.put("label",deviceLabel(device));item.put("bluetooth",BluetoothAudioRoute.isBluetooth(device));inputs.put(item);
            }
            info.put("inputs",inputs);info.put("echoAvailable",AcousticEchoCanceler.isAvailable());
            info.put("echoEnabled",echo!=null && echo.getEnabled());info.put("noiseAvailable",NoiseSuppressor.isAvailable());
            AudioDeviceInfo routed=recorder==null?null:recorder.getRoutedDevice();
            if(routed!=null){info.put("route",deviceLabel(routed));info.put("requestedMatched",matchesRequestedInput(routed));}
            info.put("verified",microphoneVerified);info.put("connecting",!startingMicId.isEmpty());
            AudioDeviceInfo output=bluetoothRoute.output();
            if(output!=null)info.put("bluetoothMode",output.getType()==AudioDeviceInfo.TYPE_BLE_HEADSET?"LE Audio":"Hands-free Bluetooth");
            info.put("ok",true);
            info.put("recording",recording);info.put("frames",microphoneFrames);
            info.put("activityAttached",activity!=null);info.put("activityVisible",foregroundVisible);info.put("backgroundVoice",backgroundVoice);
            if(Build.VERSION.SDK_INT>=29 && recorder!=null && recorder.getActiveRecordingConfiguration()!=null)
                info.put("silenced",recorder.getActiveRecordingConfiguration().isClientSilenced());
        } catch(Exception e) { return error(e); }
        return info;
    }

    private synchronized void beginMicrophone(String id) {
        if(!startingMicId.isEmpty()){reply(id,error(new Exception("The microphone is still connecting.")));return;}
        if(recording){reply(id,audioInfo());return;}
        startingMicId=id;microphoneVerified=false;requestedInput=null;
        try {
            int preferredId=prefs.getInt("micInput",0);
            selectedInputId=preferredId;
            if(preferredId>0){
                for(AudioDeviceInfo device:audioManager.getDevices(AudioManager.GET_DEVICES_INPUTS))
                    if(device.getId()==preferredId)requestedInput=device;
                if(requestedInput==null)throw new Exception("The selected microphone is unavailable. Refresh microphones and select it again.");
            }
            if(preferredId==BluetoothAudioRoute.AUTOMATIC || BluetoothAudioRoute.isBluetooth(requestedInput)) {
                bluetoothRoute.start(requestedInput,new BluetoothAudioRoute.Callback(){
                    public void ready(){openMicrophone();}
                    public void failed(Exception error){failMicrophoneStart(error);}
                });
            }else openMicrophone();
        }catch(Exception error){failMicrophoneStart(error);}
    }
    private boolean matchesRequestedInput(AudioDeviceInfo actual) {
        if(bluetoothRoute.output()!=null)return bluetoothRoute.matchesInput(actual);
        return selectedInputId==0 || (actual!=null && requestedInput!=null && actual.getId()==requestedInput.getId());
    }
    private void failMicrophoneStart(Exception error) {
        String id=startingMicId;startingMicId="";stopMicrophone();if(!id.isEmpty())reply(id,error(error));
    }
    private synchronized void openMicrophone() {
        try {
            int size = AudioRecord.getMinBufferSize(16000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
            if (size <= 0) throw new Exception("This microphone does not support 16 kHz recording.");
            recorder = new AudioRecord(bluetoothRoute.output()!=null?MediaRecorder.AudioSource.VOICE_COMMUNICATION:MediaRecorder.AudioSource.VOICE_RECOGNITION, 16000,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, Math.max(size, 6400));
            if (recorder.getState() != AudioRecord.STATE_INITIALIZED) throw new Exception("Could not open the microphone.");
            AudioDeviceInfo preferred=requestedInput;
            if(bluetoothRoute.output()!=null)for(AudioDeviceInfo device:audioManager.getDevices(AudioManager.GET_DEVICES_INPUTS))
                if(bluetoothRoute.matchesInput(device)){preferred=device;break;}
            if(preferred!=null && !recorder.setPreferredDevice(preferred))
                throw new Exception("Android declined the selected microphone. Choose another microphone in Audio settings.");
            if (prefs.getBoolean("micEcho",true) && AcousticEchoCanceler.isAvailable()) {
              try {
                echo = AcousticEchoCanceler.create(recorder.getAudioSessionId());
                if (echo != null) echo.setEnabled(true);
              } catch (Exception ignored) { echo=null; }
            }
            if (prefs.getBoolean("micNoise",false) && NoiseSuppressor.isAvailable()) {
              try {
                noiseSuppressor=NoiseSuppressor.create(recorder.getAudioSessionId());
                if(noiseSuppressor!=null)noiseSuppressor.setEnabled(true);
              } catch (Exception ignored) { noiseSuppressor=null; }
            }
            recorder.startRecording(); recording = true;
            int epoch = ++micEpoch; AudioRecord current = recorder;
            current.addOnRoutingChangedListener(route -> {
                if(recording && epoch==micEpoch && microphoneVerified){
                    if(!matchesRequestedInput(current.getRoutedDevice())){
                        microphoneVerified=false;playback.stop();stopMicrophone();
                        js("window.microphoneError && window.microphoneError('The selected microphone disconnected or Android changed its route.');");
                    }else js("window.microphoneRoute && window.microphoneRoute("+audioInfo().toString()+");");
                }
            },new Handler(Looper.getMainLooper()));
            final long deadline=android.os.SystemClock.elapsedRealtime()+5000;
            verifyMicrophone=new Runnable(){public void run(){
                if(!recording || epoch!=micEpoch)return;
                AudioDeviceInfo actual=current.getRoutedDevice();
                if(actual!=null && matchesRequestedInput(actual)){
                    microphoneVerified=true;verifyMicrophone=null;
                    String id=startingMicId;startingMicId="";reply(id,audioInfo());
                }else if(android.os.SystemClock.elapsedRealtime()>=deadline){
                    failMicrophoneStart(new Exception("Android did not activate the selected microphone"+(actual==null?".":"; it routed to "+deviceLabel(actual)+" instead.")+" Choose another microphone or reconnect the headset."));
                }else main.postDelayed(this,100);
            }};
            main.post(verifyMicrophone);
            Thread thread = new Thread(() -> {
                short[] samples = new short[320];
              try {
                while (recording && epoch == micEpoch) {
                    int n = current.read(samples, 0, samples.length, AudioRecord.READ_BLOCKING);
                    if (n <= 0) throw new IllegalStateException("Android microphone read stopped ("+n+")");
                    // Do not send audio from a fallback microphone while routing settles.
                    if(!microphoneVerified || !matchesRequestedInput(current.getRoutedDevice()))continue;
                    microphoneFrames++;
                    lastMicrophoneFrame=android.os.SystemClock.elapsedRealtime();
                    byte[] raw = new byte[n * 2];
                    if(speakerOutput&&playback.playing())outputSuppressUntil=android.os.SystemClock.elapsedRealtime()+450;
                    boolean suppress=speakerOutput&&android.os.SystemClock.elapsedRealtime()<outputSuppressUntil;
                    NativeVoiceCapture capture=voiceCapture;
                    if(capture!=null)capture.push(samples,n,playback.playing(),suppress||capturePaused);
                    for (int i = 0; i < n; i++) { short sample=suppress?0:samples[i];raw[i*2] = (byte)sample; raw[i*2+1] = (byte)(sample >> 8); }
                    String encoded = Base64.encodeToString(raw, Base64.NO_WRAP);
                    runOnUiThread(() -> { if (recording && epoch == micEpoch && !destroyed)
                        web.evaluateJavascript("window.microphoneFrame(" + JSONObject.quote(encoded) + ");", null); });
                }
              } catch (Exception e) {
                runOnUiThread(() -> {
                    if(recording && epoch==micEpoch && !destroyed){
                        if(!startingMicId.isEmpty()){failMicrophoneStart(e);return;}
                        stopMicrophone();
                        web.evaluateJavascript("window.microphoneError && window.microphoneError("+JSONObject.quote(e.getMessage()==null?"Android microphone stopped":e.getMessage())+");",null);
                    }
                });
              }
            }, "Anima microphone");
            thread.setDaemon(true); thread.start();
        } catch (Exception e) { failMicrophoneStart(e); }
    }
    private synchronized void stopMicrophone() {
        recording = false; microphoneVerified=false; micEpoch++;
        if(voiceCapture!=null)voiceCapture.reset();
        if(verifyMicrophone!=null)main.removeCallbacks(verifyMicrophone);verifyMicrophone=null;
        if (recorder != null) {
            try { recorder.stop(); } catch (Exception ignored) {}
            recorder.release(); recorder = null;
        }
        if (echo != null) { echo.release(); echo = null; }
        if (noiseSuppressor != null) { noiseSuppressor.release(); noiseSuppressor = null; }
        bluetoothRoute.stop();requestedInput=null;
        if(!startingMicId.isEmpty()){
            String id=startingMicId;startingMicId="";reply(id,error(new Exception("Microphone start cancelled.")));
        }
    }
    public void onRequestPermissionsResult(int request, String[] permissions, int[] grants) {
        if(request==33){
            requestingMicPermission=false;
            if(grants.length==0||grants[0]!=PackageManager.PERMISSION_GRANTED)js("window.phoneApp && window.phoneApp.notice('Allow Anima notifications in Android settings to use the lock-screen voice controls.');");
            if(foregroundVisible)startVoiceMicrophone(pendingMicId);else pendingPermissionStart=true;
            return;
        }
        if (request == MIC_PERMISSION) {
            requestingMicPermission = false;
            if (grants.length > 0 && grants[0] == PackageManager.PERMISSION_GRANTED) {
                if (foregroundVisible) startVoiceMicrophone(pendingMicId); else pendingPermissionStart = true;
            }
            else reply(pendingMicId, error(new Exception("Microphone permission is needed for live voice.")));
        }
    }
    public void onActivityResult(int request, int result, Intent data) {
        if (request != PICK_FILES || result != Activity.RESULT_OK || data == null) {
            if (request == PICK_FILES) js("window.filePickerFinished();");
            return;
        }
        ArrayList<Uri> uris = new ArrayList<>();
        if (data.getClipData() != null) {
            for (int i = 0; i < Math.min(6, data.getClipData().getItemCount()); i++) uris.add(data.getClipData().getItemAt(i).getUri());
        } else if (data.getData() != null) uris.add(data.getData());
        final String visit = uploadVisit;
        network.execute(() -> {
            for (Uri uri : uris) upload(uri, visit);
            js("window.filePickerFinished();");
        });
    }
    private void upload(Uri uri, String visit) {
        File temporary = null;
        try {
            String name = "shared-file";
            try (Cursor cursor = context.getContentResolver().query(uri, new String[]{OpenableColumns.DISPLAY_NAME}, null, null, null)) {
                if (cursor != null && cursor.moveToFirst()) name = cursor.getString(0);
            }
            temporary = File.createTempFile("anima-upload-", ".tmp", context.getCacheDir());
            int size = 0;
            try (InputStream in = context.getContentResolver().openInputStream(uri); OutputStream out = new FileOutputStream(temporary)) {
                byte[] block = new byte[16384]; int n;
                while ((n = in.read(block)) != -1) {
                    size += n; if (size > MAX_FILE) throw new Exception("Files must be under 50 MB.");
                    out.write(block, 0, n);
                }
            }
            if (size == 0) throw new Exception("That file is empty.");
            HttpURLConnection c = connection(validateAddress(prefs.getString("address", "")), "/api/upload", "POST", true);
            try {
                c.setDoOutput(true); c.setRequestProperty("Content-Type", "application/octet-stream");
                c.setRequestProperty("X-Visit", visit); c.setRequestProperty("X-Filename", URLEncoder.encode(name, "UTF-8").replace("+", "%20"));
                c.setFixedLengthStreamingMode(size);
                try (InputStream in = new FileInputStream(temporary); OutputStream out = c.getOutputStream()) {
                    byte[] block = new byte[16384]; int n; int sent = 0;
                    while ((n = in.read(block)) != -1) {
                        out.write(block,0,n); sent += n;
                        if (sent % 262144 < 16384) js("window.uploadProgress(" + JSONObject.quote(name) + "," + sent + "," + size + ");");
                    }
                }
                InputStream input = c.getResponseCode() >= 400 ? c.getErrorStream() : c.getInputStream();
                JSONObject response;
                try (InputStream in = input) { response = new JSONObject(new String(read(in,1000000), StandardCharsets.UTF_8)); }
                String type = context.getContentResolver().getType(uri);
                if (!response.has("error") && type != null && type.startsWith("image/")) {
                    BitmapFactory.Options bounds = new BitmapFactory.Options(); bounds.inJustDecodeBounds = true;
                    try (InputStream in = context.getContentResolver().openInputStream(uri)) { BitmapFactory.decodeStream(in,null,bounds); }
                    BitmapFactory.Options options = new BitmapFactory.Options(); options.inSampleSize = Math.max(1, Math.max(bounds.outWidth,bounds.outHeight)/400);
                    try (InputStream in = context.getContentResolver().openInputStream(uri)) {
                        Bitmap bitmap = BitmapFactory.decodeStream(in,null,options);
                        if (bitmap != null) {
                            ByteArrayOutputStream thumbnail = new ByteArrayOutputStream(); bitmap.compress(Bitmap.CompressFormat.JPEG,80,thumbnail); bitmap.recycle();
                            response.put("preview", "data:image/jpeg;base64," + Base64.encodeToString(thumbnail.toByteArray(),Base64.NO_WRAP));
                        }
                    }
                }
                js("window.fileUploaded(" + JSONObject.quote(visit) + "," + response.toString() + ");");
            } finally { c.disconnect(); }
        } catch (Exception e) { js("window.fileUploaded(" + JSONObject.quote(visit) + "," + error(e).toString() + ");"); }
        finally { if (temporary != null) temporary.delete(); }
    }
    public void onPause() {
        foregroundVisible = false;
        if (!requestingMicPermission) { js("window.appPaused && window.appPaused("+backgroundVoice+");"); if(!backgroundVoice)stopMicrophone(); }
        else js("window.permissionPaused && window.permissionPaused();");
    }
    public void onResume() {
        foregroundVisible = true;
        if (web != null) { web.onResume(); web.resumeTimers(); }
        if (pendingPermissionStart) { pendingPermissionStart = false; startVoiceMicrophone(pendingMicId); }
        if (web != null) js("window.appResumed && window.appResumed();");
    }
    public void onBackPressed() { js("window.backPressed && window.backPressed();"); }
    public void close() {
        if(destroyed)return;
        inputEpoch++;backgroundVoice=false;stopVoiceEvents();sendVoiceControl("/api/voice/stop");destroyed = true; playback.stop(); stopMicrophone(); network.shutdownNow();voiceInput.shutdownNow();
        if(audioManager!=null && deviceCallback!=null)audioManager.unregisterAudioDeviceCallback(deviceCallback);
        if (web != null) { web.removeJavascriptInterface("AnimaNative"); web.destroy(); web = null; }
    }
}
