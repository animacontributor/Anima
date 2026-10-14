# Paired Android app

The app provides text chat, file sharing, live voice, optional imagined
shared-room presence (Physicalize), and headphone positioning. The computer
runs the existing models, tools and memory; no identity, transcripts, credentials
or model weights are bundled in the APK. Android 8 or newer is required.

## Start and pair

1. Start `bat/phone.bat` (or its `.command`/`.sh` twin). The local setup page is
   `http://127.0.0.1:8780/setup`; the panel also has a **Phone app** tile.
2. Permit local-subnet access to the configured phone port if the firewall blocks
   it. On Windows the setup page offers `bat/allow-phone-wifi.ps1`; Windows asks
   for administrator permission for the narrow firewall rule.
3. Connect the phone and computer to the same local network. Use the computer
   address displayed on the setup page to download the APK, or copy it by USB.
4. Install and open Anima, enter the computer address and the pairing code shown
   on the computer, and press **Connect**. The public LAN download page does not
   expose the pairing code. Tokens are stored on the phone; the server persists
   token hashes under `memory/phone.json`.
5. Choose Physicalize before a visit if desired, then **Come in**. Use **＋** for
   photos, audio, video or documents. Files up to 50 MB are stored under `shared/`.
6. Press **Start voice** and grant microphone/notification permissions. Speech
   packages must already be installed on the computer; see [VOICE.md](VOICE.md).

The default port is `PHONE_PORT = 8780`. The phone service binds the LAN;
Parlor and the panel keep their loopback-only addresses. Keep the server window
open. End other live voice sessions before starting phone voice.

## Native voice and audio controls

Start voice while the app is visible, then lock the screen. A native foreground
service handles microphone capture, recognition submission, continuation events,
audio polling and playback receipts. Notification Interrupt/Stop and audio-focus
loss cancel native playback. Event cursors and generation guards prevent retries
or stale responses from replaying speech after interruption or a new visit.
The WebView observes events when visible; native voice works without its timers.

Headphones mode accepts speech during playback. Speaker mode gates the microphone
while output plays. Audio settings offer microphone selection, verified input,
volume and Bluetooth microphone routing. Android 12+ uses communication-device
selection; older versions use SCO. Missing, refused or timed-out routes report
an error. The app verifies the actual input and releases its communication route
when voice stops. LE Audio and hands-free quality depend on the phone/headset.

[DELIVERY.md](DELIVERY.md) describes speed, whisper blend, section pauses and
preview controls. [SPATIAL.md](SPATIAL.md) describes desktop binaural output.
Previewing these settings creates no conversation or memory turn.

Physicalize invites an imagined shared-room presence for that visit. It does not
rewrite identity or grant camera access. Transcripts and afterglow explicitly
label imagined gestures, keeping them distinct from physical events.

## Build from source

Provide Android SDK platform 35, build-tools 35.0.1 and a JDK under
`.runtime/android-build/sdk` and `.runtime/android-build/jdk`, or set
`ANIMA_ANDROID_SDK` and `ANIMA_ANDROID_JDK` to their locations. Run
`python android/build_apk.py`. The output is `.runtime/phone-app/AnimaPhone.apk`.
The build uses SDK tools directly and creates a local signing key under
`.runtime/android-build/`; keep that key private and reuse it for app updates.
Install a rebuild over the existing app to retain pairing. `--debug` creates an
emulator test APK; it is intended for isolated testing.
