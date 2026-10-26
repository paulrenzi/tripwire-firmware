#!/usr/bin/env python3
"""Convert an indoor Wi-Fi speaker test into Saturday logger session JSON.
Writes a local payload only; uploading is a separate explicit action.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics


def milliseconds(iso):
    return round(datetime.fromisoformat(iso).timestamp() * 1000)


def export(test, gateway, session, speaker_distance_ft):
    phases = json.loads((test / "runs.json").read_text())
    metadata = json.loads((test / "test.json").read_text())
    source_events = [
        json.loads(line)
        for path in sorted((test / "capture").glob("*/events.jsonl"))
        for line in path.read_text().splitlines()
    ]
    common = (
        f"Uncalibrated indoor Bluetooth replay; physical speaker-to-microphone distance "
        f"{speaker_distance_ft:g} ft. Direct XIAO Wi-Fi capture; no LoRa gateway or radio test. "
        "No simulated drone distance or calibrated SPL. Computer playback/receive timestamps; "
        "Bluetooth latency uncalibrated. Original live detector results only; no offline sensitivity alarms."
    )
    runs = []
    events = []
    for index, phase in enumerate(phases, 1):
        run_id = f"R{index:02}"
        start = milliseconds(phase["start"])
        end = milliseconds(phase["end"])
        playback = phase["phase"] == "playback"
        run = {
            "session": session,
            "run_id": run_id,
            "type": "speaker" if playback else "nodrone",
            "distance_ft": None,
            "altitude_ft": None,
            "mic_height_in": None,
            "wind": "not measured; indoors",
            "speaker_distance_ft": speaker_distance_ft,
            "phase": phase["phase"],
            "drone": f"speaker: {'DJI Mini 4 Pro' if phase['clip'] == 'mini' else '5-inch FPV'}"
            if playback
            else "",
            "notes": common
            + (
                f" Relative digital gain {phase['gain_db']} dB."
                if playback
                else f" {phase['phase']}."
            ),
            "start_ms": start,
            "start_iso": phase["start"],
            "end_ms": end,
            "end_iso": phase["end"],
            "level_dba": None,
            "cal_dba": None,
            "gain_db": phase["gain_db"],
            "duration_s": round((end - start) / 1000, 1),
        }
        runs.append(run)
        for kind, t, iso in [
            ("run_start", start, phase["start"]),
            ("run_stop", end, phase["end"]),
        ]:
            events.append(
                {
                    "session": session,
                    "run_id": run_id,
                    "source": "mark",
                    "kind": kind,
                    "t_ms": t,
                    "t_iso": iso,
                    "demo": 0,
                }
            )
    for original in source_events:
        t = milliseconds(original["received_at"])
        # Resolve membership on full timestamps, avoiding rounded boundary overlap.
        run = next(
            (
                r
                for r in runs
                if r["start_iso"] <= original["received_at"] < r["end_iso"]
            ),
            None,
        )
        event = {
            "session": session,
            "run_id": run["run_id"] if run else "",
            "t_ms": t,
            "t_iso": original["received_at"],
            "port": "wifi-direct",
            "source": "detector",
            "kind": original["type"],
            "dev": "01",
            "demo": 0,
            "transport": "wifi-direct",
        }
        if original["type"] == "event":
            event.update(
                kind=original["event"],
                conf=original.get("confidence"),
                f0bin=original.get("f0_bin"),
                rms=original.get("rms"),
            )
        elif original["type"] == "level":
            event.update(
                kind="level",
                dba=original["laeq_dba"],
                dbfs=original["rms_dbfs"],
                lzeq_db=original["lzeq_db"],
                alarm=original["alarm"],
            )
        elif original["type"] == "recording":
            event.update(file=original["file"])
        # Preserve original measurements without adding local absolute paths/audio.
        event["wifi_data"] = {
            k: v for k, v in original.items() if k not in ("received_at", "type")
        }
        events.append(event)
    events.sort(key=lambda e: e["t_ms"])
    for run in runs:
        selected = [e for e in events if e["run_id"] == run["run_id"]]
        alarms = [
            e for e in selected if e["source"] == "detector" and e["kind"] == "alarm"
        ]
        levels = [
            e for e in selected if e["source"] == "detector" and e["kind"] == "level"
        ]
        run.update(
            gw_alarms=0,
            det_alarms=len(alarms),
            heard=int(bool(alarms)),
            first_alarm_s=round((alarms[0]["t_ms"] - run["start_ms"]) / 1000, 1)
            if alarms
            else None,
            gw_alarm_s=0,
            det_alarm_s=0,
            packets=0,
            bad_packets=0,
            rssi_median=None,
            snr_median=None,
            rssi_min=None,
            max_conf=max((e["conf"] for e in alarms), default=None),
            mic_dba=None,
            det_dbfs=statistics.median(e["dbfs"] for e in levels) if levels else None,
            detector_dba_estimate_median=statistics.median(e["dba"] for e in levels)
            if levels
            else None,
            live_alarm_active_samples=sum(e["alarm"] for e in levels),
            level_samples=len(levels),
        )
        # This exporter deliberately rejects unimplemented alarm-duration semantics.
        if alarms or any(e["alarm"] for e in levels):
            raise ValueError(
                "This session has live alarms; implement and verify duration/carryover summaries before export."
            )
    return {
        "session": session,
        "gateway": gateway,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "runs": runs,
        "events": events,
        "raw": [],
        "metadata": {
            "test_kind": "uncalibrated_indoor_replay",
            "gateway_label_is_namespace_only": True,
            "transport": "wifi-direct",
            "device_id": 1,
            "physical_speaker_distance_ft": speaker_distance_ft,
            "recordings_uploaded": False,
            "offline_sensitivity_included": False,
            "notes": common,
            "sound_sources": metadata["sources"],
            "quality": json.loads((test / "quality.json").read_text()),
            "input_sha256": {
                "runs.json": hashlib.sha256(
                    (test / "runs.json").read_bytes()
                ).hexdigest()
            },
            "recording_files": [
                p.name for p in sorted((test / "capture").glob("*/*.wav"))
            ],
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("test", type=Path)
    parser.add_argument("--gateway", default="xiao01-wifi-indoor")
    parser.add_argument(
        "--session", help="default: UTC test start time plus indoor suffix"
    )
    parser.add_argument("--speaker-distance-ft", type=float, required=True)
    args = parser.parse_args()
    metadata = json.loads((args.test / "test.json").read_text())
    session = args.session or datetime.fromisoformat(metadata["started_at"]).astimezone(
        timezone.utc
    ).strftime("S%Y%m%d-%H%M%S-indoor")
    if args.speaker_distance_ft <= 0:
        parser.error("speaker distance must be positive")
    payload = export(args.test, args.gateway, session, args.speaker_distance_ft)
    path = args.test / "weekend-session.json"
    path.write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n")
    print(
        f"{path}: {len(payload['runs'])} runs, {len(payload['events'])} events, {path.stat().st_size} bytes"
    )


if __name__ == "__main__":
    main()
