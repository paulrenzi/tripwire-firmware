#!/usr/bin/env python3
"""Record a Tripwire Wi-Fi detector's events and PCM stream using stdlib only."""

import argparse
from collections import deque
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import socket
import signal
import shutil
import sys
import struct
import time
import wave

HEADER = struct.Struct("<4sBI")


def read_exact(stream, size):
    parts = bytearray()
    while len(parts) < size:
        chunk = stream.recv(size - len(parts))
        if not chunk:
            raise EOFError("device disconnected")
        parts.extend(chunk)
    return bytes(parts)


def read_packet(stream):
    magic, kind, size = HEADER.unpack(read_exact(stream, HEADER.size))
    if magic != b"TW01" or kind not in (1, 2) or size > 65536:
        raise ValueError("invalid stream header")
    payload = read_exact(stream, size)
    if kind == 2 and (len(payload) < 10 or (len(payload) - 8) % 2):
        raise ValueError("invalid PCM block")
    return kind, payload


class Console:
    """A single updating terminal screen, with a plain fallback for redirected output."""

    def __init__(
        self, live=None, host="192.168.4.1:8765", log_path=None, detection_log=None
    ):
        self.live = sys.stdout.isatty() if live is None else live
        self.host = host
        self.log_path = log_path
        self.detection_log = Path(detection_log) if detection_log else None
        self.detections = deque(maxlen=3)
        self.alarm = None
        self.confidence = None
        self.drop_baseline = None
        self.level = None
        self.last_event = "None yet"
        self.last_level = None
        self.connection = "Connecting"
        self.directory = None
        self.filename = "Waiting for audio"
        self.samples = 0
        self.gap_seconds = 0.0
        self.alarms = 0
        self.last_draw = 0.0
        self.opened = False

    def __enter__(self):
        if self.live:
            sys.stdout.write("\x1b[?1049h\x1b[?25l")
            sys.stdout.flush()
            self.opened = True
        self.render(force=True)
        return self

    def __exit__(self, *args):
        if self.opened:
            sys.stdout.write("\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()
            self.opened = False

    def status(self, message):
        self.connection = message
        logging.info(message)
        if not self.live:
            print(message, flush=True)
        self.render(force=True)

    def connected(self, directory):
        self.directory = Path(directory)
        self.connection = "CONNECTED"
        self.alarm = None
        self.confidence = None
        self.level = None
        self.last_level = None
        self.drop_baseline = None
        self.samples = 0
        self.gap_seconds = 0.0
        self.filename = "Waiting for audio"
        self.status(f"CONNECTED — {directory}")

    def detection(self, data, source):
        timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
        confidence = data.get("confidence")
        score = f"{confidence:.3f}" if confidence is not None else "unknown"
        message = (
            f"{timestamp} DRONE DETECTION | confidence={score} | source={source} | "
            f"uptime_ms={data.get('uptime_ms', 'unknown')} | "
            f"audio={self.directory / self.filename if self.directory else self.filename}"
        )
        self.alarms += 1
        self.detections.appendleft(f"{timestamp} DRONE DETECTION (confidence={score})")
        if self.detection_log:
            with self.detection_log.open("a", encoding="utf-8") as stream:
                stream.write(message + "\n")
        logging.info(message)

    def show(self, data):
        stamp = datetime.now().astimezone().strftime("%H:%M:%S")
        kind = data.get("type")
        urgent = kind in ("event", "recording")
        logging.info("%s", json.dumps(data, allow_nan=False))
        if kind == "event":
            was_active = self.alarm is True
            self.alarm = data["event"] == "alarm"
            self.confidence = data.get("confidence") if self.alarm else None
            if self.alarm and not was_active:
                self.detection(data, "event")
            self.last_event = (
                f"{stamp} — {'DRONE DETECTION' if self.alarm else 'CLEAR'}"
            )
        elif kind == "level":
            active = data["alarm"]
            urgent = active != self.alarm
            if active != self.alarm and (active or self.alarm is not None):
                if active:
                    self.detection(data, "device state")
                self.last_event = f"{stamp} — {'DRONE DETECTION' if active else 'CLEAR'} (device state)"
            self.alarm = active
            if not active:
                self.confidence = None
            self.level = data
            self.last_level = time.monotonic()
            if self.drop_baseline is None:
                self.drop_baseline = data.get("audio_drops", 0)
        elif kind == "recording":
            self.filename = data["file"]
        elif kind == "audio_gap":
            self.gap_seconds += data["seconds"]
        if not self.live:
            if kind == "event":
                print(
                    f"[{stamp}] {self.last_event} | confidence={data.get('confidence', 0):.3f}",
                    flush=True,
                )
            elif kind == "level":
                print(
                    f"[{stamp}] {'DRONE ALERT' if self.alarm else 'LISTENING'} "
                    f"{data['rms_dbfs']:.1f} dBFS | LAeq≈{data['laeq_dba']:.1f} dBA",
                    flush=True,
                )
            elif kind == "recording":
                print(f"[{stamp}] Saving audio: {self.filename}", flush=True)
        self.render(force=urgent)

    def lines(self):
        stale = self.last_level is not None and time.monotonic() - self.last_level > 3
        connected = self.connection.startswith("CONNECTED")
        if not connected:
            state = "WAITING FOR CONNECTION"
        elif self.alarm is None or stale:
            state = "WAITING FOR FRESH DETECTOR STATE"
        else:
            state = (
                ">>> DRONE DETECTION <<<"
                if self.alarm
                else "LISTENING — no active drone alert"
            )
        level = self.level
        dbfs = level["rms_dbfs"] if level else -120.0
        filled = max(0, min(36, round((dbfs + 90) / 90 * 36))) if level else 0
        meter = "#" * filled + "." * (36 - filled)
        age = (
            f"{time.monotonic() - self.last_level:.1f}s ago"
            if self.last_level
            else "waiting"
        )
        drops = (
            max(0, level.get("audio_drops", 0) - (self.drop_baseline or 0))
            if level
            else 0
        )
        return [
            "TRIPWIRE | LIVE MICROPHONE MONITOR",
            datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
            "",
            state,
            "",
            f"Sound       [{meter}] {dbfs:.1f} dBFS"
            if level
            else f"Sound       [{meter}] waiting",
            f"Level       LAeq ≈ {level['laeq_dba']:.1f} dBA | LZeq ≈ {level.get('lzeq_db', 0):.1f} dB"
            if level
            else "Level       waiting",
            f"Confidence  {self.confidence:.3f} (last alert)"
            if self.confidence is not None
            else "Confidence  —",
            f"Detections  {self.alarms} this run | Most recent first:",
            *[
                (self.detections[i] if i < len(self.detections) else "—")
                for i in range(3)
            ],
            "",
            f"Connection  {self.connection.split(' — ')[0]} | {self.host}",
            f"Last level  {age}",
            f"Audio       {self.samples / 16000:.1f}s received | {self.gap_seconds:.3f}s missing",
            f"Health      {drops} dropped blocks since connection | {level.get('dma_overflows', 0) if level else 0} DMA overruns",
            "",
            f"Recording   {self.filename if connected else 'Paused; waiting for connection'}",
            f"Folder      {self.directory or 'Waiting for device'}",
            f"Logs        {self.log_path or 'events.jsonl in recording folder'}",
            "",
            "Ctrl+C: stop and save | Acoustic candidates; dBA estimates are uncalibrated.",
        ]

    def render(self, force=False):
        if not self.live or (not force and time.monotonic() - self.last_draw < 0.25):
            return
        self.last_draw = time.monotonic()
        size = shutil.get_terminal_size((100, 24))
        # Sanitize terminal controls from error messages/paths before drawing.
        lines = [
            "".join(c if c.isprintable() else " " for c in line)[
                : max(1, size.columns - 1)
            ]
            for line in self.lines()[: max(1, size.lines - 1)]
        ]
        sys.stdout.write(
            "\x1b[H" + "\n".join(line + "\x1b[K" for line in lines) + "\x1b[J"
        )
        sys.stdout.flush()


class Recorder:
    """Rotate WAV files, preserving device sample offsets across network gaps."""

    def __init__(self, directory, segment_seconds=60, console=None):
        self.console = console
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.limit = segment_seconds * 16000
        self.index = 0
        self.wav = None
        self.frames = 0
        self.expected = None
        self.start = None
        self.events = (self.directory / "events.jsonl").open("a", buffering=1)

    def log(self, data):
        self.events.write(
            json.dumps(
                {"received_at": datetime.now(timezone.utc).isoformat(), **data},
                allow_nan=False,
            )
            + "\n"
        )

    def _write(self, pcm, offset):
        while pcm:
            if self.wav is None:
                name = f"audio-{self.index:05d}.wav"
                self.wav = wave.open(str(self.directory / name), "wb")
                self.wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                self.frames = 0
                record = {"type": "recording", "file": name, "sample_offset": offset}
                self.log(record)
                if self.console:
                    self.console.show(record)
                self.index += 1
            count = min(len(pcm) // 2, self.limit - self.frames)
            self.wav.writeframesraw(pcm[: count * 2])
            self.frames += count
            offset += count
            pcm = pcm[count * 2 :]
            if self.frames == self.limit:
                self.wav.close()
                self.wav = None

    def audio(self, payload):
        offset = struct.unpack_from("<Q", payload)[0]
        pcm = payload[8:]
        if self.expected is None:
            self.start = offset
            self.expected = offset
        if offset < self.expected:
            raise ValueError("audio sample offset moved backwards")
        missing = offset - self.expected
        if missing:
            # Bound insertion; a long outage starts a fresh connection/session.
            if missing > 16000 * 30:
                raise ValueError("audio gap exceeds 30 seconds; starting new session")
            self.log(
                {
                    "type": "audio_gap",
                    "sample_offset": self.expected,
                    "missing_samples": missing,
                    "seconds": missing / 16000,
                }
            )
            if self.console:
                self.console.show({"type": "audio_gap", "seconds": missing / 16000})
            self._write(bytes(missing * 2), self.expected)
        self._write(pcm, offset)
        self.expected = offset + len(pcm) // 2
        if self.console:
            self.console.samples += len(pcm) // 2
            self.console.render()

    def close(self):
        if self.wav:
            self.wav.close()
            self.wav = None
        self.events.close()


def capture(host, port, output, segment_seconds, duration=0, console=None):
    session = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    recorder = None
    with socket.create_connection((host, port), timeout=10) as stream:
        stream.settimeout(10)
        kind, payload = read_packet(stream)
        hello = json.loads(payload) if kind == 1 else {}
        if (
            hello.get("type"),
            hello.get("sample_rate"),
            hello.get("channels"),
            hello.get("sample_width"),
        ) != ("hello", 16000, 1, 2):
            raise ValueError("unsupported device audio format")
        directory = Path(output) / session
        console = console or Console()
        console.connected(directory)
        recorder = Recorder(directory, segment_seconds, console)
        started = time.monotonic()
        try:
            recorder.log(hello)
            logging.info("Device hello: %s", json.dumps(hello))
            while not duration or time.monotonic() - started < duration:
                kind, payload = read_packet(stream)
                if kind == 2:
                    recorder.audio(payload)
                else:
                    data = json.loads(payload)
                    recorder.log(data)
                    console.show(data)
        finally:
            recorder.close()


def main():
    # Finalize the active WAV when stopped by a process manager as well as Ctrl+C.
    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "host",
        nargs="?",
        default="192.168.4.1",
        help="device IP (192.168.4.1 in access point mode)",
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output", default="recordings")
    parser.add_argument("--segment-seconds", type=int, default=60)
    parser.add_argument(
        "--duration",
        type=float,
        default=0,
        help="stop after N seconds; default continuous",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="show simulated terminal output, without connecting or recording",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="print scrolling output instead of a live dashboard",
    )
    args = parser.parse_args()
    if args.segment_seconds < 1 or args.duration < 0:
        parser.error("segment length must be positive and duration nonnegative")
    if args.demo:
        with Console(
            live=False if args.plain else None, host="DEMO — simulated data"
        ) as console:
            console.connected("DEMO — no recordings or logs written")
            items = [
                {"type": "level", "alarm": False, "rms_dbfs": -48.5, "laeq_dba": 55.0},
                {"type": "event", "event": "alarm", "confidence": 0.82},
                {"type": "level", "alarm": True, "rms_dbfs": -28.0, "laeq_dba": 74.0},
                {"type": "event", "event": "clear", "confidence": 0.10},
            ]
            try:
                for item in items:
                    console.show(item)
                    if console.live:
                        time.sleep(2)
            except KeyboardInterrupt:
                pass
        return
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    log_path = output.resolve() / datetime.now(timezone.utc).strftime(
        "monitor-%Y%m%dT%H%M%S.%fZ.log"
    )
    logging.basicConfig(
        filename=log_path,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )
    with Console(
        live=False if args.plain else None,
        host=f"{args.host}:{args.port}",
        log_path=log_path,
        detection_log=output / "detections.log",
    ) as console:
        try:
            while True:
                try:
                    console.status("Connecting")
                    capture(
                        args.host,
                        args.port,
                        args.output,
                        args.segment_seconds,
                        args.duration,
                        console,
                    )
                    break
                except (OSError, EOFError, ValueError, KeyError) as error:
                    logging.exception("Connection ended")
                    console.status(f"DISCONNECTED — {error}; retrying in 3 seconds")
                    if args.duration:
                        raise SystemExit(1) from error
                    time.sleep(3)
        except KeyboardInterrupt:
            logging.info("Stopped by user; WAV headers finalized")
    print(
        f"Recording stopped. Audio/events: {output.resolve()}\nDetailed log: {log_path}",
        flush=True,
    )


if __name__ == "__main__":
    main()
