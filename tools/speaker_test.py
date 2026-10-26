#!/usr/bin/env python3
"""Short, uncalibrated indoor replay check through a Bluetooth speaker.
Uses locally downloaded/decoded weekend-test clips; records exclusively during
this foreground run. Requires numpy, paplay and the XIAO Wi-Fi connection.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import statistics
import subprocess
import threading
import time
import wave

import numpy as np
import wifi_listener

RATE = 16000


def utc():
    return datetime.now(timezone.utc).isoformat()


def render(source, destination, seconds, gain_db):
    with wave.open(str(source)) as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, RATE):
            raise ValueError("Expected decoded mono PCM16 16 kHz source")
        data = (
            np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(
                np.float64
            )
            / 32768
        )
    # Match the page's trim, peak normalization, equal-power loop splice, and ramps.
    trim = int(0.05 * RATE)
    fade = min(int(0.5 * RATE), (len(data) - 2 * trim) // 4)
    n = len(data) - 2 * trim - fade
    if n < RATE or not np.max(np.abs(data)):
        raise ValueError("Clip too short or silent")
    data *= 10 ** (-1 / 20) / np.max(np.abs(data))
    loop = data[trim : trim + n].copy()
    angle = np.arange(fade) / fade * np.pi / 2
    loop[:fade] += data[trim : trim + fade] * (np.sin(angle) - 1) + data[
        trim + n : trim + n + fade
    ] * np.cos(angle)
    out = np.resize(loop, int(seconds * RATE)) * 10 ** (gain_db / 20)
    ramp = min(int(0.5 * RATE), len(out) // 2)
    out[:ramp] *= np.linspace(0, 1, ramp)
    out[-ramp:] *= np.linspace(1, 0, ramp)
    with wave.open(str(destination), "wb") as wav:
        wav.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        wav.writeframes((np.clip(out, -1, 1) * 32767).astype("<i2").tobytes())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.4.1")
    parser.add_argument(
        "--sink", required=True, help="Explicit PulseAudio speaker sink"
    )
    parser.add_argument(
        "--assets", type=Path, default=Path("recordings/speaker-test-assets")
    )
    parser.add_argument("--output", type=Path, default=Path("recordings/speaker-tests"))
    args = parser.parse_args()
    directory = args.output / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory.mkdir(parents=True)
    plan = [("mini", gain) for gain in (-24, -12, -6)] + [
        ("fpv", gain) for gain in (-24, -12, -6)
    ]
    sources = {}
    for clip in ("mini", "fpv"):
        sources[clip] = {
            "url": f"https://paulrenzi.github.io/tripwire/tools/weekend/sounds/{clip}.mp3",
            "sha256": hashlib.sha256(
                (args.assets / f"{clip}.mp3").read_bytes()
            ).hexdigest(),
        }
    for clip, gain in plan:
        render(
            args.assets / f"{clip}.wav",
            directory / f"playback-{clip}-{gain}.wav",
            12,
            gain,
        )
    metadata = {
        "kind": "uncalibrated_indoor_replay",
        "page": "https://paulrenzi.github.io/tripwire/weekend-test.html",
        "speaker_sink": args.sink,
        "sources": sources,
        "started_at": utc(),
        "note": "Relative digital gains only; no distance/SPL calibration or radio-range test. "
        "Timing uses computer receive/playback times; Bluetooth latency is not calibrated.",
    }
    (directory / "test.json").write_text(json.dumps(metadata, indent=2))
    logging.basicConfig(
        filename=directory / "monitor.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    errors = []
    ready = threading.Event()
    stop = threading.Event()

    class TestConsole(wifi_listener.Console):
        def connected(self, folder):
            super().connected(folder)
            ready.set()

        def show(self, data):
            # Console is intentionally quiet except detections during the test.
            if data.get("type") == "event":
                print(
                    f"{utc()} DETECTOR {data['event'].upper()} confidence={data.get('confidence')}",
                    flush=True,
                )
            super().show(data)

        def render(self, force=False):
            pass

    console = TestConsole(live=True, detection_log=directory / "detections.log")

    def record():
        try:
            # Capture is bounded to this test; cancellation closes the stream on
            # the next packet via the console's render hook.
            original_render = console.render

            def checked_render(force=False):
                if stop.is_set():
                    raise KeyboardInterrupt
                original_render(force)

            console.render = checked_render
            wifi_listener.capture(
                args.host, 8765, directory / "capture", 60, 180, console
            )
        except KeyboardInterrupt:
            pass
        except Exception as error:
            errors.append(str(error))
            ready.set()

    thread = threading.Thread(target=record, daemon=True)
    thread.start()
    print(f"Test folder: {directory.resolve()}", flush=True)
    runs = []

    def phase(name, seconds, clip=None, gain=None):
        if errors or not thread.is_alive():
            raise RuntimeError(f"Recorder stopped: {errors}")
        entry = {"phase": name, "clip": clip, "gain_db": gain, "start": utc()}
        print(
            f"{entry['start']} {name} {clip or ''} {str(gain) + ' dB' if gain is not None else ''} ({seconds}s)",
            flush=True,
        )
        if clip:
            subprocess.run(
                [
                    "paplay",
                    "--device=" + args.sink,
                    str(directory / f"playback-{clip}-{gain}.wav"),
                ],
                check=True,
            )
        else:
            time.sleep(seconds)
        entry["end"] = utc()
        runs.append(entry)
        (directory / "runs.json").write_text(json.dumps(runs, indent=2))

    try:
        if not ready.wait(15) or errors:
            raise RuntimeError(
                f"Cannot start recorder: {errors or 'connection timeout'}"
            )
        phase("initial_background", 10)
        for clip, gain in plan:
            phase("playback", 12, clip, gain)
            phase("quiet_gap", 6)
        phase("final_background", 20)
    finally:
        stop.set()
        thread.join(timeout=12)
    if errors or thread.is_alive():
        raise RuntimeError(f"Recording did not finish cleanly: {errors}")
    events = []
    for path in (directory / "capture").glob("*/events.jsonl"):
        events.extend(json.loads(line) for line in path.read_text().splitlines())
    summary = []
    for run in runs:
        selected = [
            event
            for event in events
            if run["start"] <= event["received_at"] <= run["end"]
        ]
        levels = [event for event in selected if event["type"] == "level"]
        alarms = [
            event
            for event in selected
            if event["type"] == "event" and event.get("event") == "alarm"
        ]
        row = {
            **run,
            "alarm_transitions": len(alarms),
            "alarm_active_level_samples": sum(bool(x["alarm"]) for x in levels),
            "level_samples": len(levels),
            "median_dbfs": round(statistics.median(x["rms_dbfs"] for x in levels), 2)
            if levels
            else None,
            "gaps": sum(x["type"] == "audio_gap" for x in selected),
        }
        summary.append(row)
    (directory / "results.json").write_text(json.dumps(summary, indent=2))
    print("\nRESULTS (indoor replay; no range claim)", flush=True)
    for run in summary:
        if run["phase"] != "quiet_gap":
            print(
                f"{run['phase']:20} {run['clip'] or 'background':5} {str(run['gain_db']):>4} dB | "
                f"median {run['median_dbfs']} dBFS | new alarms {run['alarm_transitions']} | "
                f"alarm-active samples {run['alarm_active_level_samples']}/{run['level_samples']}",
                flush=True,
            )
    print(
        f"Finished. Recordings, labels and results: {directory.resolve()}", flush=True
    )


if __name__ == "__main__":
    main()
