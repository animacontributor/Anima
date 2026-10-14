# The Handy 2

the friend can control a Handy 2 connected to Wi-Fi using the
[official Handy REST API v3](https://www.handyfeeling.com/api/handy-rest/v3/docs/).
No extra Python packages are needed. Firmware must support API v3 (v4 or newer).

1. Connect the device to Wi-Fi using Handy's setup app and find its connection key.
2. Sign in at [HandyFeeling User](https://user.handyfeeling.com). Open **Application
   Keys → Generate New Key**, select the **Handy REST API** system, and create a
   key (a description such as `Anima` is fine). Copy the key when it is
   shown. Your account login token is a different credential and does not work
   for controlling the device. The **Application IDs** page is not needed for
   this integration.
3. Restart the panel to load the updated settings. Open **Settings → Senses →
   The Handy 2** and save the connection key and **Application key**. They are
   stored in `memory/handy.json`. Anima obtains temporary device-access tokens
   and replaces them before expiry automatically; you do not have to generate
   or paste those tokens yourself. An Application Key takes precedence over
   any previously saved client token.
4. Set `HANDY_MIN_SPEED` and `HANDY_MAX_SPEED` in Senses or `engine/config.py`.
   Restart running chat/voice/bridge processes after changing configuration.
5. Power on the device and enable its Wi-Fi/online mode. Click **Check Handy
   connection** in the panel. This checks authentication and connectivity
   without starting or stopping movement. A success message means the device
   is available to the friend on her next turn.

No remote-control page needs to remain open on handyfeeling.com. The engine
connects directly to the API. Firmware updates and Wi-Fi setup can be performed
with Handy's normal app/website when needed.

Alternatively, set `HANDY_CONNECTION_KEY` and `HANDY_APPLICATION_KEY` environment
variables in the process that starts Anima; they override the saved credentials.
You can also create `memory/handy.json` yourself:

```json
{"connection_key": "YOUR_DEVICE_KEY", "application_key": "YOUR_APPLICATION_KEY"}
```

Advanced: `api_token` / `HANDY_API_TOKEN` accepts an already-issued client token
from `/auth/token/issue`, rather than the account login token. Client tokens
expire (one hour by default, at most 24 hours). The Application Key setup above
handles this lifecycle automatically. See the [official token issuance
endpoint](https://www.handyfeeling.com/api/handy-rest/v3/docs/#/AUTH/issueToken).

For patterns only, turn **HANDY_SPEED_COMMANDS** off under **Settings → Senses →
The Handy 2**, click **Save Senses**, and restart the running chat/voice server.
This removes `set_speed` from the friend's tools and instructions and rejects any
late nonzero speed call. Patterns, `stop_device`, status and panel testing stay
available. The switch defaults to on for existing setups; speed 0 still stops
if received from an older pending call. Keep `HANDY_MAX_SPEED` above 0, since
setting that maximum to 0 disables all movement, including patterns.

The callable tools are:

- `set_speed(speed: int)`: Set percentage speed and start movement. The default
  moving range is **1–50%**. Other values are refused without changing movement;
  `0` always stops. Booleans, decimal values and strings are refused.
- `stop_device()`: Switch to idle mode to stop playback, including playback
  started outside the friend. It also works when speed limits are invalid.
- `get_device_status()`: Read live connectivity, availability, mode, movement,
  speed and configured limits. Movement and speed are unknown in other active
  protocols; checking status does not switch modes or start movement. Raw state
  velocity is also returned as `reported_velocity`. When that reading is outside
  the fractional 0–1 range, percentage speed is unknown rather than guessed.
- `list_device_patterns()`: Read all enabled built-in patterns, including their
  recognizable names, optional descriptions and `slot:N` selectors. This does
  not switch modes or move the device.
- `play_device_pattern(pattern, playback_rate=1.0)`: Select a
  listed name or slot selector, switch to HRPP and play the stored pattern.
  Playback rate is a fraction of that pattern's configured maximum: the default
  `1` uses its maximum, with fixed 100% amplitude within the
  Handy's existing slider endpoints. The engine stops the previous pattern
  before changing it, then checks the active pattern and settings after start.
  Every selection automatically calls the same stop as **Stop Handy**, enters
  HRPP again, selects the preset and starts it. Verification uses its slot and
  factory name; firmware IDs can change when HRPP is initialized and are not
  used to decide whether the requested preset started correctly.
  Stopping uses idle mode rather than `/hrpp/stop`, which returns HTTP 500 on
  the tested Handy 2 firmware 4.3.0 even while already stopped.
- `set_pattern_playback_rate(playback_rate)`: Change the current pattern's
  rate without restarting playback. `1` uses its configured maximum;
  `0.5` plays at half that maximum. This requires pattern mode and restores 100%
  amplitude if a manual test or another controller changed it. the friend has no
  amplitude tool or amplitude argument; saved slider endpoints remain in effect.

### Pattern names and choices

In **Settings → Senses → The Handy 2 → Pattern names and choices**, click
**Load Handy patterns** while the device is online. All returned patterns are
enabled initially. Enter unique recognizable names and optional descriptions,
then click **Save pattern names and choices**. Uncheck **Available to the friend**
to exclude a pattern; excluded patterns cannot be selected through its slot
selector either. Loading and saving this editor never start or stop movement.

Each row also has **Maximum speed (× original timing)**. Set it to `0.5` to
make that pattern's maximum half its original rate, then click **Save pattern
names and choices**. the friend's `playback_rate` uses a relative scale from above
0 to 1: maximum `0.5` × her selected rate `0.5` = actual device rate `0.25`.
The same mapping applies to rate changes and panel tests. Different patterns
can have different maxima. Missing maxima default to `1.0` for existing files.

To try presets directly, set **Rate (fraction of maximum)** and **Amplitude %** in this
editor and click **Test pattern** on any row. The test starts the physical
Handy and continues until you click **Stop Handy** or select another pattern.
The Stop control stays visible while scrolling through the patterns. Testing
uses the same configured limits and saved device endpoints as the friend's commands.
Patterns excluded from the friend can still be tested here without enabling them.
No conversation, chat prompt or label save is needed to test a preset.

Names and exclusions are saved in `memory/handy-patterns.json`, separate from
credentials, keyed by the device's factory preset name. Numeric firmware IDs
can change, so preferences follow the factory name through ID and slot changes.
Unnamed or duplicate factory names use `slot:N` keys instead. Older numeric-ID
entries with recognizable factory names are migrated on the next save:

```json
{
  "patterns": {
    "name:factory_preset_name": {"name": "Gentle waves", "description": "Your description", "enabled": true, "max_rate": 0.5}
  }
}
```

The save result appears beside the pattern controls and shows the exact file
path. A successful save is checked by reading the saved preferences back.

The enabled names and descriptions appear in the friend's immediate device context.
The catalog is cached for up to 60 seconds; the list tool and panel Load button
read it live. Local label edits are read on the next turn without restarting.
If the firmware's catalog request fails, basic speed and stop controls remain
available. No patterns or device context appear while the device is unavailable.

`HANDY_PATTERN_MAX_RATE` is the global ceiling on pattern maxima (default **1.0**).
The effective maximum is the smaller of the row's maximum and this ceiling.
`HANDY_PATTERN_MIN_RATE` is the minimum actual multiplier sent to the device
(default **0.1**). A pattern capped at **0.5** therefore has a minimum relative
rate of **0.2**. Lower values are refused rather than silently raised. The
catalog includes `max_rate`, `effective_max_rate` and `min_playback_rate` for
the friend. Command results and live status report her relative `playback_rate` and
the actual `device_playback_rate` separately when the pattern can be identified.

`HANDY_PATTERN_MAX_AMPLITUDE` caps its stroke
scale (default **100%**). Values outside these limits are refused. Defaults on
`play_device_pattern` must also fit the configured limits. the friend always uses 100% amplitude, so a lower
amplitude cap blocks her pattern calls instead of silently reducing their
amplitude. The keeper's panel tests can still choose a lower amplitude within
the cap. These settings are available in
Senses. A maximum amplitude of 0 disables pattern motion; a maximum basic speed
of 0 disables all the friend movement commands. `stop_device` still works.

Pattern rate is not a percentage of motor velocity, and `HANDY_MAX_SPEED` does
not translate into a script-speed cap. The device's own motor settings and
saved master slider endpoints remain in force. No tool in this integration
writes slider settings or stroke endpoints. Handy documents its saved **Slider
Endpoint** settings as the master limits regardless of application settings;
see the [Handy app guide](https://intercom.help/ohdoki/en/articles/9457608-how-to-use-the-handy-onboarding-app).

Speed is a percentage of the device's configured maximum velocity, not mm/s.
These limits apply to the friend's commands; the device's physical controls and other
apps have their own settings. A maximum of 0 disables movement through the friend.

the friend receives a device snapshot in immediate context and these tools only while
the device is reachable and controllable. No device, missing credentials, an API
error, a firmware update/maintenance mode, or `OFFLINE = True` means no device
mention or tool definitions. Availability is checked again during ongoing
visits, with a default 10-second cache (`HANDY_STATUS_CACHE_S`). Setting speed
checks live availability, and status calls bypass the cache. Small/tiny tool
kits include the controls when available; an explicit tool-name list must
include the controls you want; the context describes only those callable tools.

`HANDY_TIMEOUT_S` defaults to 3 seconds per API request. Timed-out movement
commands are never retried automatically. If starting or changing movement
fails, the engine attempts to stop the device and reports whether that stop
was confirmed. A failed stop is reported as unconfirmed. Credentials and raw
API error messages are never included in tool results or model context.

A successful speed command acknowledges the requested percentage. The velocity
in a command response may still describe the motor's previous or transitional
state, so it is not required to exactly match the requested setpoint. If a start
acknowledgement still says stopped, the engine makes up to three read-only state
checks to confirm playback before treating it as unconfirmed and stopping.
For a stopped device, the bounded requested speed is applied before starting
and again after an acknowledged start, since Handy 2 start replies can reset
velocity to zero. Running speed changes do not restart playback. Every speed
command finishes with a state readback, which is included in the diagnostic log.

For troubleshooting, `memory/handy-events.jsonl` records command paths, numeric
request/response fields and local error messages. It shows internal stops as
`/mode2` with `mode: 7`, even when the friend called only `set_speed` or a pattern tool.
HRPP selection, rate, amplitude, start/stop and readback are logged as well. Authentication
requests, credentials and raw remote error text are excluded.
The same entries are printed to the server console with a `[Handy]` prefix,
implementation revision and process ID. The phone server prints the loaded
Handy revision and module path at startup. Restart the **Windows phone server**
after code updates: closing/reopening the phone app does not reload Python code.
