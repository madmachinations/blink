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
| Apple Home integration | Camera streaming with listening audio and linked doorbell/motion entities through HA's HomeKit Bridge. |
| Camera thumbnails | Blink's latest stored image displayed without waking the camera, plus an action to take a fresh snapshot. |
| USB and cloud recordings | Discover and download clips from Sync Module USB storage, Blink cloud storage, or both. |
| Optional recording archive | Automatic downloads, configurable local retention and playback in HA's Media browser. |
| Motion sensors | A Motion binary sensor per camera, inferred from newly available recordings so not live events. That is being worked on. |
| Camera controls | Enable or disable motion detection on individual cameras and doorbells. |
| System controls | Arm or disarm each Blink system. |
| Doorbell buttons | A "ring doorbell" button in HA, and an optional ring-on-motion-detected setting per doorbell which will trigger that the doorbell was pressed. This can be automated via an Alexa routine currently, work is ongoing for actual live doorbell button press events directly from blink. |
| Automation events | Events for new clips, successful downloads, inferred motion and locally reported doorbell presses. |
| Browser-based sign-in | Blink-hosted login and MFA, token refresh and HA reauthentication. |

## Current limitations and work in progress

- **Motion is inferred from completed recordings.** Detection includes recording
  completion and polling delay. A recording's cause is not verified, so manually
  requested recordings can also count as motion.
- **Direct physical doorbell press detection and real-time motion notifications
  are still being worked on**, pending further hardware validation. Currently,
  doorbell events can come from the local button, an automation/Alexa fallback,
  or explicitly enabled Ring on motion routing.
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

| Setting | Default | Purpose |
| --- | --- | --- |
| Recording sources | `usb` | Choose `usb`, `cloud`, or `all` for both. Controls discovery and inferred motion. |
| Automatic downloads | Off | Archive newly discovered recordings when enabled. |
| Archive directory | Empty | Text field requiring the full absolute filesystem path to the archive folder, as seen by the running HA process. For a folder inside your HA configuration directory, use that directory's actual absolute path: `/config/videos` if HA's configuration is at `/config`. The archive folder must already exist, be writable and persistent, and be included in `allowlist_external_dirs`. A separate persistent directory outside the configuration directory is also supported. |
| Retention | 30 days | Keep local copies for this many days after saving; `0` retains indefinitely. |
| Poll interval | 60 seconds | Refresh devices and discover completed recordings; minimum 30 seconds. |
| Maximum live session | 60 seconds | Limit live sessions; maximum 300 seconds. |
| Motion sensor reset delay | 60 seconds | Keep motion on after discovery; new clips extend the timer. Range 1–3600 seconds. |

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

A new recording turns its camera's Motion sensor on; it resets after the configured
delay. Another new clip extends the timer. Expect a delay in these motion events because it cannot trigger until the new recording has finished being created by Blink.

Motion sensors expose `detection_source: recording`, `inferred: true`, `occurred_at`
and `observed_at`. Use `blink_live_motion_detected` for repeated reports while the
sensor is already on.

For doorbells, **Ring on motion** sends future inferred motion through the Press
event. Again this will be delayed, but exists as a feature that might work for some to ring the doorbell when someone has knocked instead. 

Enabling it while motion is already on will not cause the doorbell to be "pressed".

The Ring doorbell button works independently of that switch and creates fresh button pressed events for each doorbell:

```yaml
action: button.press
target:
  entity_id: button.blink_live_front_door_ring_doorbell
```

This exists to use an existing Alexa fallback for getting doorbell pressed signals out of Blink,xpose the

Expose the HA button as a scene and use an Alexa routine triggered by the actual Blink
doorbell press to activate it. This fallback still depends on Alexa; direct Blink press detection is not
yet available.

## Credentials

OAuth credentials are stored in the config entry
under `/config/.storage/core.config_entries`; your Blink password is not stored.
Protect access to the config volume and backups. Use HA's reauthentication prompt
to sign in again if authorization expires.

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
| `blink_live_motion_detected` | Activity was inferred from a newly discovered recording. |
| `blink_live_doorbell_pressed` | A press was reported through a button/automation or enabled motion routing. |

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

`clips`, `watch` and `event-log` accept `--source usb|cloud|all`; USB is the default.
`watch` polls rather than receiving push notifications, with a minimum interval of
30 seconds. Retention `0` keeps files indefinitely. Historical first-baseline clips
are not automatically downloaded. Ctrl-C stops the command and cleans up a preview.

`diagnose DEVICE` checks a bounded live session without a player.
`event-log --output FILE.jsonl` saves timestamped polling observations to a new,
private file; it is not a push listener.

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
