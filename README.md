# Home Assistant Blink Camera Live Video Integration

**Live video, recordings and Blink camera controls for Home Assistant.**

Bring on-demand Blink camera video with listening audio into Home Assistant,
alongside snapshots, USB and cloud recordings, motion sensors and system controls.

Blink Live is standalone and uses the `blink_live` domain. It can run alongside
HA's official Blink integration with separate credentials, devices and entities.

## Features

| Feature | What you get |
| --- | --- |
| Live video and audio | On-demand live viewing, with a shared upstream session when multiple viewers open the same camera. |
| Optional RTSP server | Authenticated, on-demand H.264 video and AAC listening audio for Scrypted and other RTSP-over-TCP viewers. Disabled by default. |
| HTTP event requests | Generic, templated HTTP requests for doorbell presses, motion detection and motion clearing, filtered by camera. Disabled by default. |
| Apple Home integration | Camera streaming with listening audio and linked doorbell/motion entities through HA's HomeKit Bridge. |
| Camera thumbnails | Blink's latest stored image displayed without waking the camera, plus an action to take a fresh snapshot. |
| USB and cloud recordings | Discover and download clips from Sync Module USB storage, Blink cloud storage, or both. |
| Optional recording archive | Automatic downloads, configurable local retention and playback in HA's Media browser. |
| Motion sensors | Live notification-driven Motion sensors per camera; recording-derived detection remains available when live events are disabled. |
| Camera controls | Enable or disable motion detection on individual cameras and doorbells. |
| System controls | Arm or disarm each Blink system. |
| Live events (experimental) | Bundled FCM receiver for physical doorbell presses and motion, enabled by default; no extra container. |
| Doorbell buttons | A "ring doorbell" button in HA, and an optional ring-on-motion-detected setting per doorbell which will trigger that the doorbell was pressed. This can be automated via an Alexa routine currently, direct Blink push reception is enabled by default. |
| Automation events | Events for new clips, successful downloads, inferred motion and locally reported doorbell presses. |
| Browser-based sign-in | Blink-hosted login and MFA, token refresh and HA reauthentication. |

## Current limitations and work in progress

- **Live motion and doorbell events are enabled by default.** Physical testing has
  confirmed live motion reception; compatibility still depends on camera models
  and Blink's cloud protocol. A connected Google transport alone does not establish
  event delivery for a particular account.
- **With live events disabled, motion is inferred from completed recordings.**
  Detection then includes recording completion and polling delay. A recording's
  cause is not verified, so manually requested recordings can also count as motion.
- Two-way talk, HomeKit Secure Video and continuous recording are not provided.
  Live sessions default to 60 seconds and can be configured up to 300 seconds.
- Blink connectivity is required, including for USB retrieval. This is not a
  direct local connection to the cameras or USB drive. Cloud recordings must
  already be accessible to your Blink account; this integration does not provide
  a cloud storage subscription.

Blink's APIs are private and may change.

## Install in Home Assistant

### Requirements

- Home Assistant **2026.10 or newer**.
- FFmpeg available inside HA. The integration uses HA's FFmpeg integration;
  a separate media sidecar is not required.
- A Blink account and outbound connectivity to Blink.
- Active Sync Module USB storage for USB clips, or accessible cloud recordings
  for cloud retrieval.
- A writable persistent directory if you want to archive downloaded recordings.

The official Blink integration can remain enabled. Blink Live signs in separately.

### Manual installation

1. Copy `custom_components/blink_live` into `/config/custom_components/` in your HA
   installation. Alternatively, download `blink-live-homeassistant.zip` from a
   GitHub Release and extract it into `/config/`.
2. Restart Home Assistant.
3. Open **Settings → Devices & services → Add integration → Blink Live**.
4. Follow the Blink sign-in link, complete login/MFA, and paste the final callback
   URL into the HA form. Enter any additional verification code requested.
5. Open the new Blink Live devices to find cameras, sensors, buttons and switches.

The callback starts with `https://applinks.blink.com/signin/callback`. A failed
final browser page is expected; copy its URL from the address bar. If Safari or
the Blink app hides the callback, use a desktop browser such as Firefox. Do not
share callback URLs or credential files.

### HACS installation

1. Add [madmachinations/blink](https://github.com/madmachinations/blink) as a custom
   **Integration** repository in HACS.
2. Download Blink Live and restart HA.
3. Follow manual installation steps 3–5 above.

### Configure recordings and playback

Initial setup handles sign-in. Afterwards, open **Settings → Devices & integrations →
Blink Live → Configure/Options** to change the settings below. Saving options
reloads the integration. These settings are not supplied in `configuration.yaml`.
Choose **Recordings and playback** for the settings in this table. RTSP and HTTP
event delivery have their own options sections and can be enabled independently.

| Setting | Default | Purpose |
| --- | --- | --- |
| Recording sources | `usb` | Choose `usb`, `cloud`, or `all` for both. Controls discovery and inferred motion. |
| Automatic downloads | Off | Archive newly discovered recordings when enabled. |
| Archive directory | Empty | Text field requiring the full absolute filesystem path to the archive folder, as seen by the running HA process. For a folder inside your HA configuration directory, use that directory's actual absolute path: `/config/videos` if HA's configuration is at `/config`. The archive folder must already exist, be writable and persistent, and be included in `allowlist_external_dirs`. A separate persistent directory outside the configuration directory is also supported. |
| Retention | 30 days | Keep local copies for this many days after saving; `0` retains indefinitely. |
| Poll interval | 60 seconds | Refresh devices and discover completed recordings; minimum 30 seconds. |
| Maximum live session | 60 seconds | Limit live sessions; maximum 300 seconds. |
| Recording-derived motion reset delay | 60 seconds | Keep motion on after recording discovery; new clips extend the timer. Range 1–3600 seconds. |
| Live motion expiry | 15 seconds | With live events enabled, clear motion after this time without another accepted motion notification for that camera. Configure under Live events; range 1–3600 seconds. |

Discovery and motion sensors work with downloads off. Each newly enabled source
establishes a historical baseline: existing clips remain available for explicit
download, but are not announced as new or automatically archived.

**Retention deletes only local files downloaded and indexed by Blink Live. It
never deletes USB or cloud originals.** Scheduled pruning runs while automatic
archiving is enabled; disabling automatic downloads also stops scheduled pruning.
Age is measured from local save time. Unrelated or replaced files are left alone;
Blink's own cloud expiration policy still applies.

## Cameras, motion and doorbells

### Live viewing and thumbnails

Open a camera in HA to start live viewing. Up to four local viewers can share one
Blink session per camera. Sessions close on disconnect, failure or duration expiry.
HA may retain its input until its own idle timeout, but the configured maximum
duration still applies.

**Do not enable Preload stream** on these camera entities. Preloading or dashboard
reconnections can repeatedly wake battery cameras. Prefer thumbnail cards that
open live view when selected.

To take a new photo, use **Developer tools → Actions → Blink Live: Take snapshot**,
or an automation:

```yaml
action: blink_live.take_snapshot
data:
  entity_id: camera.blink_live_front_door
```

This wakes the selected camera, waits for capture and refreshes its image cache.
A failed capture preserves the previous image. It starts no live stream and saves
no file. To save the image, follow it with HA's `camera.snapshot` action using an
allowed destination. `camera.snapshot` alone saves the cached image without taking
a new photo.

### System and camera controls

| Entity | What it controls |
| --- | --- |
| System **Armed** switch | On arms that Blink system/network; Off disarms it. |
| Camera **Motion detection** switch | Enables/disables that camera's actual Blink motion setting. |
| Camera **Motion** sensor | Reports activity inferred from a newly discovered recording. |
| Doorbell **Ring on motion** switch | Triggers a doorbell pressed event if the doorbell detects motion. |
| Doorbell **Ring doorbell** button | Reports a press locally for HA/HomeKit; does not operate the physical chime. |

Motion recording generally requires **both an armed system and an enabled camera**.
Each system has its own device and switch. Enabling one camera does not arm the
system or enable other cameras. Use normal HA actions with your actual entity IDs:

```yaml
# Arm a system. Use switch.turn_off to disarm.
action: switch.turn_on
target:
  entity_id: switch.blink_live_system_home_armed
```

```yaml
# Disable a camera's motion detection. Use switch.turn_on to enable.
action: switch.turn_off
target:
  entity_id: switch.blink_live_front_door_motion_detection
```

### Motion and doorbell behavior

With live events disabled, a new recording turns its camera's Motion sensor on; it resets after the configured
delay. Another new clip extends the timer. Expect a delay in these motion events because it cannot trigger until the new recording has finished being created by Blink.

Motion sensors expose `detection_source: recording`, `inferred: true`, `occurred_at`
and `observed_at`. Use `blink_live_motion_detected` for repeated reports while the
sensor is already on.

For doorbells, **Ring on motion** routes accepted motion through the Press event.
This uses live motion when enabled; recording-derived motion includes recording
and polling delays.

Enabling it while motion is already on will not cause the doorbell to be "pressed".

The Ring doorbell button works independently of that switch and creates fresh button pressed events for each doorbell:

```yaml
action: button.press
target:
  entity_id: button.blink_live_front_door_ring_doorbell
```

This exists to use an existing Alexa fallback for getting doorbell pressed signals out of Blink,xpose the

Expose the HA button as a scene and use an Alexa routine triggered by the actual Blink
doorbell press to activate it. This fallback still depends on Alexa. The experimental
live receiver below provides an alternative that needs testing with your account.

### Live events (experimental)

**Live events are enabled by default.** Open **Configure → Live events
(experimental)** to change this setting or adjust the motion expiry. An explicit
saved opt-out is preserved. Existing entries without a saved preference enable
live events on their next reload or HA restart.
The integration registers an independent Firebase/Android identity, associates it
with its existing Blink login, and listens for doorbell presses and motion through
Google's push service. There is no additional container, Firebase project, webhook,
command-line registration, or separate login to configure. HA needs outbound HTTPS
and TCP 5228 to `mtalk.google.com`. This remains cloud-dependent.

A diagnostic **Blink Live events connection** binary sensor shows transport status
and the last accepted event time. **Connected proves only the Google connection**;
verify that Blink actually sends events to this account session. Persistent failures
also appear in Repairs. Firebase credentials and FCM tokens are refreshed and
re-associated automatically; expired Blink authorization uses the existing reauth flow.

In live mode, motion has `detection_source: blink_push` and `inferred: false`.
Each accepted motion notification turns the sensor on and restarts a per-camera
expiry, **15 seconds from receipt** by default. If no further motion notification
arrives, the sensor clears. Adjust **Live motion expiry** in Live events options
for cameras that notify less frequently. This is an estimate based on recent
notifications, not an explicit motion-ended signal; delayed or missing pushes can
clear the sensor while movement continues. Both basic motion reports such as
`motion/cam2sm_motion` and camera motion reports such as `media/cv_motion` restart
the same expiry: whichever arrived most recently keeps motion active. Duplicate
deliveries and unrecognized notifications do not extend the expiry. Recording-derived
motion keeps its separate reset delay (60 seconds by default).
Physical presses use `source: blink_push` with a stable event ID and provider time.
Existing motion sensors, Press entities, HomeKit links, event-bus automations and
HTTP event rules receive these events. Recording discovery and downloads continue,
but recordings no longer generate inferred motion, including during push outages.
Disable Live events to restore recording-derived motion. Manual ringing and the
optional Ring on motion setting continue to work.

The receiver rejects events older than two minutes, undated events, mismatched
accounts/regions and ambiguous devices. IDs take precedence over camera names;
name-only pushes require an exact unique match. Recent IDs persist across restarts
to suppress replay. A crash after saving an ID but before dispatch can lose that
event; this is not an exactly-once delivery guarantee. Blink and Google's
undocumented Android protocols may change.

Before relying on live events:

1. Press the physical button without triggering motion; check for one Press event.
2. Trigger motion without pressing; check the right camera's Motion sensor and reset.
3. Repeat both, including on other cameras; confirm events arrive before clip discovery.
4. Restart HA and interrupt/reconnect the network; check recovery without old rings.
5. Confirm the Blink mobile app still receives its own notifications.

Our automated tests are offline. Live reception using Blink Live's existing Android OAuth session has been verified
on a physical camera; use these checks to validate additional devices and accounts. Upstream
reports successful reception on doorbell, Outdoor 3 and Mini Pan-Tilt hardware;
that does not establish compatibility with every account or model.

## Credentials

OAuth credentials are stored in the config entry
under `/config/.storage/core.config_entries`; your Blink password is not stored.
Protect access to the config volume and backups. Use HA's reauthentication prompt
to sign in again if authorization expires. The separate private push identity and
recent delivery IDs are stored under `/config/.storage/blink_live.<entry_id>.push`.
Raw push payloads are not logged or retained.

RTSP credentials and HTTP rule templates (including any endpoint tokens or header
secrets you enter) are also stored in the config entry. Passwords are masked in the
RTSP form; request templates remain visible to HA administrators when editing a
rule. These values are excluded from integration diagnostics and routine logs.

## Automation actions and events

| Action | Purpose |
| --- | --- |
| `blink_live.take_snapshot` | Take a new thumbnail for a camera and refresh its cached image. |
| `blink_live.refresh_clips` | Discover recordings now; optionally supply `entry_id` to select an account. |
| `blink_live.download_clip` | Download a known clip using camera `entity_id`, stable `clip_id`, and optional allowed `directory`. Returns `path` and `clip_id`. |

| Event | Meaning |
| --- | --- |
| `blink_live_clip_available` | A previously unseen USB/cloud recording was discovered. |
| `blink_live_clip_saved` | A complete local recording and archive index were committed. |
| `blink_live_motion_detected` | Motion arrived through live push, or was inferred from a recording with live events disabled. |
| `blink_live_motion_cleared` | The motion reset timer expired for a camera. |
| `blink_live_doorbell_pressed` | A press arrived through live push, a button/automation, or enabled motion routing. |

Each camera also has a Recording event entity with `clip_available` and `clip_saved`
types. Use it for recording automations.

## Apple Home through HomeKit Bridge

Use HA's HomeKit Bridge in **accessory mode**, with one instance per camera and a
unique port. Enable listening audio and link the doorbell's Press event to its camera:

```yaml
homekit:
  - name: Blink Front Door
    mode: accessory
    port: 21064
    filter:
      include_entities:
        - camera.blink_live_front_door
    entity_config:
      camera.blink_live_front_door:
        support_audio: true
        linked_doorbell_sensor: event.blink_live_front_door_press
        # Optional recording-derived motion notifications:
        # linked_motion_sensor: binary_sensor.blink_live_front_door_motion

  - name: Blink Back Camera
    mode: accessory
    port: 21065
    filter:
      include_entities:
        - camera.blink_live_back
    entity_config:
      camera.blink_live_back:
        support_audio: true
```

## Optional RTSP server

Open **Settings → Devices & services → Blink Live → Configure/Options → RTSP
server**. Set a username and password, enable the server, and save. It is **off by
default** and uses the FFmpeg already available to HA; no sidecar is required.

| Setting | Default | Purpose |
| --- | --- | --- |
| Enable RTSP server | Off | Listen for authenticated RTSP viewers. |
| Listen address | `0.0.0.0` | Bind to the HA process's IPv4 network interfaces. Use a specific local interface address to restrict listening. |
| Listen port | `8554` | TCP port. Each enabled Blink Live account needs a different port. |
| Username / password | Empty | Both are required when enabled. Choose separate RTSP credentials; these are not your Blink login. |

Every discovered camera has a stable path using its Blink Live device key. Find
the credential-free `rtsp_url` attribute in **Developer tools → States** for the
camera, replace `HA-HOST` with HA's reachable hostname/IP, and enter the RTSP
credentials in your player. With the default port, for example:

```text
rtsp://HA-HOST:8554/123456_doorbell_789012
rtsp://HA-HOST:8554/123456_camera_345678
```

If the player requires credentials in the address:

```text
rtsp://viewer:YOUR-RTSP-PASSWORD@192.168.1.20:8554/123456_doorbell_789012
```

URL-encode special characters in embedded credentials. To test from a laptop:

```sh
ffplay -rtsp_transport tcp 'rtsp://viewer:YOUR-RTSP-PASSWORD@192.168.1.20:8554/123456_doorbell_789012'
```

Only **RTSP 1.0 with RTP/RTCP interleaved over TCP** is supported. Video remains
H.264 without another video encode; audio is AAC-LC using MPEG4-GENERIC RTP
packetization. Audio is omitted if the input has none. The RTSP audio packetizer
encodes audio to provide codec headers expected by common clients. Up to four
RTSP clients per camera share one packetizer and one gateway subscription; the
gateway allows four subscriptions total across RTSP and HA/HomeKit viewers.
There is a limit of 16 simultaneous RTSP connections per account.

Starting the listener or inspecting camera addresses does not wake cameras.
Opening/describing a particular stream can wake it to obtain its media description.
The existing maximum session duration still applies, including when HA was already
viewing that camera. Expiry disconnects RTSP viewers; Blink Live does not
automatically restart their sessions. A new request can start a new session.
**Disable prebuffering, continuous recording, and automatic reconnect loops in
downstream applications** to avoid repeatedly waking battery cameras. RTSP has no
camera directory or discovery API; add each address manually. ONVIF is not included.

This listener uses Basic authentication and **does not encrypt credentials or
media**. Keep it on a trusted private network; do not expose it to the internet.

### Network ports

Addresses and ports refer to the network namespace in which HA runs. With Docker
bridge networking, publish the configured port, for example `8554:8554/tcp` in
addition to your existing HA ports. With host networking, use the container host's
reachable IP and allow that TCP port through the host firewall.

Allow viewers to reach the configured RTSP TCP port (default **8554**). No UDP
ports need to be exposed. Use HA's reachable hostname or IP in stream addresses.
A viewer in a different container must use an address that reaches HA;
`127.0.0.1` normally refers to the viewer's own container.

## Optional HTTP event requests

Open **Configure/Options → HTTP event requests**, enable delivery, select **Add
rule**, then submit. Add up to 32 named rules. Select **Edit rule** to change or
disable one, or **Delete rule** to remove it. Saving a rule also saves the master
enable setting; simply opening or editing a form sends no requests.

Each rule selects an event, cameras (empty means all), method, URL, headers, and
body. Supported methods are GET, POST, PUT, PATCH, and DELETE. Events are:

| Rule event | When it fires |
| --- | --- |
| `doorbell_pressed` | Each accepted press: local button/Alexa fallback, enabled Ring on motion, or an accepted Blink push. |
| `motion_detected` | Each newly accepted motion report, including repeated reports while the sensor is on. From push in live mode; otherwise inferred from recordings. |
| `motion_cleared` | The camera's motion timeout expires; subsequent detections extend this timer. |

Existing HA events and entities continue working independently. Initial recording
baselines, restored states, startup, and unload send no synthetic detections or
presses. Direct realtime Blink event reception is still work in progress.

URL, header **values**, and body support HA/Jinja templates. Headers must be a JSON
object with string values; leave `{}` for no custom headers. Leave the body empty
for no body. Available variables are:

| Variable | Meaning |
| --- | --- |
| `camera_key` / `camera_name` | Stable Blink device key / current display name. |
| `entity_id` | The HA camera entity ID. |
| `event_type` / `event_id` | One of the rule events above / identifier of this report. |
| `source` | `recording`, `manual`, `motion` for Ring on motion, or `blink_push` for a direct push. |
| `occurred_at` / `observed_at` | Original occurrence time when known (otherwise null) / integration observation time, as ISO timestamps. |
| `motion_active` | `true` for detection, `false` for clearing, null for a press. |
| `event` | Original event data, including account/network/camera identifiers and any inference attributes. |

For a generic JSON receiver, select **POST** and use, for example:

URL:

```jinja
http://event-receiver:8080/cameras/{{ camera_key | urlencode }}/events
```

Headers:

```json
{"Content-Type": "application/json", "Authorization": "Bearer YOUR-ENDPOINT-TOKEN"}
```

Body:

```jinja
{{ {"camera": camera_key, "name": camera_name, "entity_id": entity_id, "type": event_type, "id": event_id, "source": source, "motion": motion_active, "time": occurred_at, "observed_at": observed_at} | to_json }}
```

Use `to_json` for JSON escaping and `urlencode` for URL path/query values. Templates
can also map camera keys to different endpoint device IDs or tokens, though separate
camera-specific rules are usually simpler.

Requests are best effort, queued in order per camera, with a five-second timeout
per request, 32 queued reports per camera, and at most 32 active camera workers.
Slow endpoints can delay later requests; full queues drop new reports. There is no
retry or replay after restart, no redirect following, and no response-body download.
HTTPS certificate verification stays enabled. For a receiver with a self-signed
certificate, establish trusted HTTPS or use its private-network HTTP endpoint.
Failures do not interrupt HA events. Integration diagnostics contain rule IDs and
last delivery status, without request details or secrets.

### Example: Scrypted without MQTT or a custom plugin

1. Install Scrypted's existing **RTSP Camera**, **Dummy Switch**, and **Webhook**
   plugins. Add each camera manually using its Blink Live RTSP address and
   credentials. Select TCP transport. Disable prebuffering and any automatic
   continuous streaming; if needed, choose Scrypted's FFmpeg RTSP parser for
   audio compatibility.
2. Create a separate dummy switch for each camera's motion. Enable **Custom Motion
   Sensor** on the camera and select that dummy switch as its motion sensor.
3. For a doorbell, create another dummy switch. Enable **Custom Doorbell Button**
   on that camera and select the doorbell switch. Configure this switch to reset
   after one second, so later presses create a new activation. Keep separate
   switches for motion and presses.
4. Enable the **Webhook** extension on each dummy switch. In its Webhook settings,
   create a webhook for **OnOff**, then copy the **Local Base URL** (trusted HTTPS)
   or **Insecure Local Base URL** (HTTP) from the device console. Append `/turnOn`
   or `/turnOff` to that URL. Use the generated device IDs/tokens, not Blink keys.
5. Add the following camera-specific HTTP rules in Blink Live. Use **GET**, headers
   `{}`, and an empty body. Replace the example host, port, device IDs, and tokens
   with the URLs Scrypted generated:

| Blink Live event | Example URL |
| --- | --- |
| `doorbell_pressed` | `http://scrypted:11080/endpoint/@scrypted/webhook/DOORBELL-SWITCH-ID/TOKEN/turnOn` |
| `motion_detected` | `http://scrypted:11080/endpoint/@scrypted/webhook/MOTION-SWITCH-ID/TOKEN/turnOn` |
| `motion_cleared` | `http://scrypted:11080/endpoint/@scrypted/webhook/MOTION-SWITCH-ID/TOKEN/turnOff` |

Set the motion dummy switch's **Reset Sensor** to `0` when using explicit clearing,
or use a finite reset as a fallback if a clearing request is lost. The doorbell
dummy switch should retain its short reset. Pressing the HA **Ring doorbell** button
tests the press path; this does not verify a physical Blink press. Presses faster
than the dummy switch's reset may merge into one Scrypted activation.

Configure these camera associations before pairing through Scrypted's HomeKit
plugin. Scrypted handles its Apple Home presentation; Blink Live does not implement
HKSV. Recording-derived motion arrives after Blink has finished recording, so it
cannot provide timely HKSV recording triggers. Physical Scrypted/HomeKit playback,
notifications, and recording behaviour still need validation with your devices.

See Scrypted's [camera setup](https://docs.scrypted.app/add-camera.html),
[Webhook implementation](https://github.com/koush/scrypted/tree/main/plugins/webhook),
and [Dummy Switch implementation](https://github.com/koush/scrypted/tree/main/plugins/dummy-switch)
for the existing endpoint and sensor capabilities used by this example.

## Development CLI client

This is not for general use, more for development purposes and testing.

The bundled Python client runs independently of HA. It supports macOS/Linux with
Python **3.13+**. Live preview also requires FFmpeg **including ffplay**. From the
project root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
# macOS, if FFmpeg is missing:
brew install ffmpeg

.venv/bin/blink-live login
.venv/bin/blink-live devices
.venv/bin/blink-live live 'Front Door' --duration 60
.venv/bin/blink-live clips --source all
mkdir -p recordings
.venv/bin/blink-live download CLIP_ID --directory ./recordings
```

Alternatively, download `blink_live_cli-VERSION-py3-none-any.whl` from a GitHub
Release and install it inside a Python 3.13+ virtual environment:

```sh
python -m pip install ./blink_live_cli-VERSION-py3-none-any.whl
blink-live login
```

Use the actual downloaded filename in place of `VERSION`. The version and
`py3-none-any` portions are required by Python's wheel naming format.

For login, finish the printed browser sign-in flow and paste the final callback URL
into the hidden terminal prompt. Device keys from `devices` are unambiguous; unique
names also match without regard to case. `download` uses the stable ID from `clips`
and automatically selects its USB/cloud source.

```sh
# Watch for new clips without saving video:
.venv/bin/blink-live watch --source all

# Automatically archive new clips, retaining local copies for 30 days:
.venv/bin/blink-live watch --source all --directory ./recordings --retention-days 30
```

`clips` and `watch` accept `--source usb|cloud|all`; USB is the default.
`watch` polls rather than receiving push notifications, with a minimum interval of
30 seconds. Retention `0` keeps files indefinitely. Historical first-baseline clips
are not automatically downloaded. Ctrl-C stops the command and cleans up a preview.

`diagnose DEVICE` checks a bounded live session without a player.

```sh
# Capture for five minutes (the output file must not already exist):
.venv/bin/blink-live notification-log --output notifications.jsonl --duration 300

# Include original payloads and binary message fields for protocol investigation:
.venv/bin/blink-live notification-log --output notifications-raw.jsonl --raw
```

FCM state and recent delivery IDs are stored in `~/.blink-live/auth-push.json`,
separately from the CLI login and recording metadata.

Press `d` then Enter to mark a physical doorbell press, `m` for motion, or `b` for
baseline. Use `--no-interactive` when running without a terminal. Ctrl-C or
`--duration` closes the receiver and flushes the log.

Each JSONL row includes UTC receipt time and elapsed time. `notification_received`
records the signal before filtering; `notification_result` records acceptance or
an explicit reason such as `unknown_event_type`, `ambiguous_device`,
`device_not_found`, `account_mismatch`, `stale`, or `duplicate`. Accepted events,
connection states, receiver errors and notification receipt outcomes are logged
separately. Unknown payload key names remain visible even in normal capture.

Output files are created with owner-only permissions and never overwrite an
existing file. Normal capture retains selected protocol/device fields, omitting
arbitrary values and credentials. `--raw` deliberately includes unredacted push
payloads and original protobuf fields (binary fields use base64); keep that file
private and redact it before sharing. Registration credentials and login frames
are never included. These captures are development evidence, not HA events.

Credentials are stored in `~/.blink-live/auth.json` with owner-only permissions;
recording metadata is stored separately beside it. One CLI process may use that
credential file at a time. Put `--state /another/private/auth.json` before the
command to use a separate identity. Windows is not currently supported by the
client's POSIX credential lock.

## Inspiration, attribution and licence

Blink Live was inspired by
[sealad886/homebridge-blink-cameras-new-api](https://github.com/sealad886/homebridge-blink-cameras-new-api),
the Homebridge plugin by Dr Andrew Cox. Its work on Blink's API and live-stream
transport provided the foundation for investigating live viewing in Home Assistant.

This is a separate Python implementation. It does not load files from the original
repository or require Homebridge. Adapted IMMIS protocol code and public vendor
trust certificates retain the upstream copyright and MIT licence text in the bundled
[NOTICE](custom_components/blink_live/blink_live_client/NOTICE). Blink Live's own
licence is [MIT](LICENSE).

Live event functionality is based on work done by Corey Hayward with [ha-blink-live-events](https://github.com/CoreyHayward/ha-blink-live-events)
