"""Small development CLI; never imports Home Assistant."""

import argparse
import asyncio
import contextlib
import getpass
import json
import shutil
import sys
from pathlib import Path

import aiohttp

from .client import BlinkClient
from .errors import BlinkError, VerificationRequired
from .gateway import StreamGateway, stop_process
from .immis import ImmisStream
from .notification_log import log_notifications
from .recordings import Recordings
from .storage import load_json, save_json


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Blink Live development client")
    result.add_argument(
        "--state",
        type=Path,
        default=Path.home() / ".blink-live" / "auth.json",
        help="Private CLI login state (separate from HA)",
    )
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="Sign in on Blink's hosted page")
    commands.add_parser("devices", help="List camera keys, names and models")
    diagnose = commands.add_parser("diagnose", help="Test one live session without a video player")
    diagnose.add_argument("device", help="Camera key, name or serial")
    live = commands.add_parser("live", help="Open an on-demand ffplay preview")
    live.add_argument("device", help="Camera key, name or serial")
    live.add_argument("--duration", type=int, choices=range(1, 301), default=60, metavar="1..300")
    live.add_argument("--ffmpeg", default="ffmpeg")
    live.add_argument("--ffplay", default="ffplay")
    clips = commands.add_parser("clips", help="Discover and list recordings")
    download = commands.add_parser("download", help="Download by stable clip ID from clips")
    download.add_argument("clip")
    download.add_argument("--directory", type=Path, required=True)
    watch = commands.add_parser("watch", help="Watch for new recordings; polling, not push")
    watch.add_argument("--interval", type=int, default=60)
    watch.add_argument("--directory", type=Path, help="Opt into automatic downloads here")
    watch.add_argument("--retention-days", type=int, default=30)
    notifications = commands.add_parser(
        "notification-log", help="Log live Blink notifications, decisions and physical test markers"
    )
    notifications.add_argument(
        "--output", type=Path, required=True, help="New JSONL file (must not exist)"
    )
    notifications.add_argument("--duration", type=int, help="Stop after this many seconds")
    notifications.add_argument(
        "--no-interactive", action="store_true", help="Disable keyboard markers"
    )
    notifications.add_argument(
        "--raw", action="store_true", help="Include unredacted push payloads in the private file"
    )
    for command in (clips, watch):
        command.add_argument("--source", choices=("usb", "cloud", "all"), default="usb")
    return result


async def initialize(client: BlinkClient) -> None:
    while True:
        try:
            await client.initialize()
            return
        except VerificationRequired as error:
            await client.send_pin(error.kind)
            code = await asyncio.to_thread(
                getpass.getpass, f"Blink {error.kind} verification PIN: "
            )
            await client.verify_pin(error.kind, code)


async def run(args: argparse.Namespace) -> None:
    state = await load_json(args.state)

    async def save_auth(value):
        await save_json(args.state, value)

    async with aiohttp.ClientSession() as http:
        client = BlinkClient(http, state, save_auth)
        if args.command == "login":
            print("Open this URL in a browser and finish Blink sign-in and MFA:\n")
            print(client.auth.begin_login())
            print("\nCopy the final https://applinks.blink.com/signin/callback?... URL.")
            callback = await asyncio.to_thread(getpass.getpass, "Callback URL (hidden): ")
            await client.auth.complete_login(callback.strip())
            await initialize(client)
            print(f"Signed in. Found {len(client.devices)} cameras.")
            return
        await initialize(client)
        if args.command == "notification-log":
            await log_notifications(
                client,
                args.output,
                args.state.with_name(args.state.stem + "-push.json"),
                args.duration,
                not args.no_interactive,
                raw=args.raw,
            )
            return
        if args.command == "devices":
            for device in client.devices.values():
                print(
                    f"{device.key}\t{device.name}\t{device.model}\t"
                    f"{'online' if device.online else 'offline'}"
                )
            for module in client.modules.values():
                print(
                    f"USB {module.id}\t{module.name}\t"
                    f"{'active' if module.local_storage else 'unavailable'}"
                )
            return
        if args.command == "diagnose":
            device = client.device(args.device)
            print("Requesting one live-view session; endpoint and credentials are withheld.")
            async with asyncio.timeout(40):
                response = await client.liveview(device)
                async with ImmisStream(response["server"], device.serial) as transport:
                    print("TLS peer identity and trust verified; IMMIS authentication sent.")
                    packets = transport.packets()
                    try:
                        payload = await anext(packets)
                        print(f"Received verified-session MPEG-TS media ({len(payload)} bytes).")
                    finally:
                        await packets.aclose()
            return
        if args.command == "live":
            ffmpeg, ffplay = shutil.which(args.ffmpeg), shutil.which(args.ffplay)
            if not ffmpeg or not ffplay:
                raise BlinkError("Install FFmpeg with ffplay (on macOS: brew install ffmpeg)")
            gateway = StreamGateway(client, ffmpeg, args.duration)
            process = None
            device = client.device(args.device)
            await gateway.start()
            try:
                process = await asyncio.create_subprocess_exec(
                    ffplay,
                    "-hide_banner",
                    "-loglevel",
                    "quiet",
                    "-autoexit",
                    "-window_title",
                    "Blink Live",
                    gateway.url(device),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await process.wait()
            finally:
                if process:
                    await stop_process(process)
                await gateway.close()
            result = gateway.results.get(device.key)
            if result and result["first_media_seconds"] is None:
                raise BlinkError(result["failure"] or "No live video received")
            if result and result["first_media_seconds"] is not None:
                print(f"First media received in {result['first_media_seconds']:.2f}s")
            return
        recordings_path = args.state.with_name(args.state.stem + "-recordings.json")

        async def save_recordings(value):
            await save_json(recordings_path, value)

        async def notify(kind, data):
            print(json.dumps({"event": f"blink_live_{kind}", **data}), flush=True)

        directory = getattr(args, "directory", None)
        if directory:
            await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True, mode=0o700)
        recordings = Recordings(
            client,
            await load_json(recordings_path),
            save_recordings,
            notify,
            directory=directory,
            automatic=args.command == "watch" and directory is not None,
            retention_days=getattr(args, "retention_days", 30),
            recording_source=getattr(args, "source", "usb"),
        )
        if args.command == "download":
            print(await recordings.download(args.clip, directory))
            return
        await recordings.refresh()
        if args.command == "clips":
            for key, clip in recordings.clips.items():
                if args.source != "all" and clip.source != args.source:
                    continue
                print(f"{key}\t{clip.camera_key}\t{clip.created_at}\t{clip.size}\t{clip.source}")
            return
        if args.interval < 30 or args.retention_days < 0:
            raise BlinkError("Minimum poll interval is 30 seconds; retention must be nonnegative")
        print(f"Watching {args.source} recordings (polling). Ctrl-C to stop.", flush=True)
        delay = args.interval
        while True:
            await asyncio.sleep(delay)
            try:
                await client.discover()
                await recordings.refresh()
                delay = args.interval
            except BlinkError:
                delay = min(900, delay * 2)
                print(f"Recording refresh failed; retrying in {delay}s", file=sys.stderr)


def main() -> None:
    args = parser().parse_args()
    try:
        # One process per credential file prevents refresh-token rotation races.
        with credential_lock(args.state):
            asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except BlinkError as error:
        print(f"Blink Live: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, ValueError, KeyError):
        print(
            "Blink Live failed. Check sign-in, devices, storage, paths, and dependencies."
            " No credentials were logged.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


@contextlib.contextmanager
def credential_lock(path: Path):
    import fcntl

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a", opener=_private_open) as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BlinkError("Another Blink Live process is using this credential file") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _private_open(path, flags):
    import os

    return os.open(path, flags | os.O_NOFOLLOW, 0o600)


if __name__ == "__main__":
    main()
