#!/usr/bin/env python3
"""Offline threshold sweep of a labeled Wi-Fi speaker test; never flashes hardware.
NumPy FFT on recorded PCM16, firmware-equivalent harmonic scan and alarm state
machine. Checks the harmonic implementation against compiled firmware C.
"""

import argparse
import ctypes
from datetime import datetime
import hashlib
import itertools
import json
from pathlib import Path
import subprocess
import tempfile
import wave

import numpy as np

N, RATE = 1024, 16000


def features(samples):
    frames = samples[: len(samples) // N * N].reshape(-1, N) / 32768.0
    windowed = frames * np.hanning(N)
    rms = np.sqrt(np.mean(windowed**2, axis=1))
    psd = np.abs(np.fft.rfft(windowed, axis=1)) ** 2
    peak_bin = np.argmax(psd[:, 12:154], axis=1) + 12
    peak = psd[np.arange(len(psd)), peak_bin]
    nf = np.maximum(1e-18, psd[:, 1:].mean(axis=1))
    ratios = []
    for mult in (2, 3):
        bins = np.clip(peak_bin[:, None] * mult + np.arange(-2, 3), 0, 512)
        ratios.append(
            psd[np.arange(len(psd))[:, None], bins].max(axis=1) / (peak + 1e-18)
        )
    return rms, peak_bin, peak / nf, ratios[0], ratios[1], psd


def confidence(snr, h2, h3, min_snr=4, min_h2=0.07, min_h3=0.035):
    eligible = snr >= min_snr
    harmonic = eligible & (h2 >= min_h2) & (h3 >= min_h3)
    good = np.minimum(1, snr / 25 * np.sqrt(h2 * h3))
    bad = np.minimum(
        0.99, np.minimum(1, snr / 40) * np.maximum(h2 / min_h2, h3 / min_h3)
    )
    return harmonic, np.where(eligible, np.where(harmonic, good, bad), 0)


def replay(rms, harmonic, conf, on=0.3, off=0.18, gate=0.00003, sustain=2):
    ema = 0.0
    active = False
    on_count = off_count = 0
    alarms = np.zeros(len(rms), dtype=bool)
    starts = np.zeros(len(rms), dtype=bool)
    smooth = np.zeros(len(rms))
    for i in range(len(rms)):
        before = active
        if rms[i] < gate:
            ema *= 0.75
            if active:
                off_count += 1
                if off_count >= 8:
                    active = False
        else:
            ema = 0.25 * (conf[i] if harmonic[i] else min(conf[i], 0.15)) + 0.75 * ema
            if not active:
                if harmonic[i] and ema >= on:
                    on_count += 1
                    off_count = 0
                    if on_count >= sustain:
                        active = True
                else:
                    on_count = 0
            else:
                if not harmonic[i] or ema < off:
                    off_count += 1
                    on_count = 0
                    if off_count >= 8:
                        active = False
                else:
                    off_count = 0
        starts[i] = active and not before
        alarms[i] = active
        smooth[i] = ema
    return alarms, starts, smooth


def verify_c(psd, expected_ok, expected_conf, root, harmonic_scale=1.0, min_snr=4.0):
    # Compile the actual harmonic-scan section, with only ESP type headers
    # stubbed. NumPy supplies PSD; this does not emulate ESP-DSP's FFT rounding.
    class Result(ctypes.Structure):
        _fields_ = [("fundamental_bin", ctypes.c_int)] + [
            (key, ctypes.c_float)
            for key in (
                "fundamental_hz",
                "fundamental_pwr",
                "harmonic2_pwr",
                "harmonic3_pwr",
                "h2_ratio",
                "h3_ratio",
                "noise_floor",
                "snr",
                "confidence",
            )
        ]

    source = (root / "main/audio_processor.c").read_text()
    tail = source[source.index("/* --- Harmonic scan") :]
    nf_start = source.index(
        "__attribute__((weak)) float audio_processor_noise_floor_estimate"
    )
    nf_end = source.index("\nstatic void build_hanning", nf_start)
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary)
        (path / "esp_err.h").write_text("typedef int esp_err_t;\n")
        generated = (
            f"#define AUDIO_PROC_HARM_PEAK_MIN_SNR {min_snr}f\n"
            f"#define AUDIO_PROC_HARM_MIN_H2 {0.07 * harmonic_scale}f\n"
            f"#define AUDIO_PROC_HARM_MIN_H3 {0.035 * harmonic_scale}f\n"
            '#include <math.h>\n#include <string.h>\n#include "audio_processor.h"\n'
            + source[nf_start:nf_end]
            + tail
        )
        (path / "scan.c").write_text(generated)
        subprocess.run(
            [
                "cc",
                "-shared",
                "-fPIC",
                "-O2",
                "-I" + str(path),
                "-I" + str(root / "main"),
                str(path / "scan.c"),
                "-lm",
                "-o",
                str(path / "scan.so"),
            ],
            check=True,
        )
        lib = ctypes.CDLL(str(path / "scan.so"))
        lib.analyze_harmonics.argtypes = [
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_int,
            ctypes.c_float,
            ctypes.c_float,
            ctypes.POINTER(Result),
        ]
        lib.analyze_harmonics.restype = ctypes.c_bool
        array = np.ascontiguousarray(psd, dtype=np.float32)
        mismatches = 0
        max_error = 0.0
        for i, row in enumerate(array):
            out = Result()
            ok = lib.analyze_harmonics(
                row.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                513,
                180,
                2400,
                ctypes.byref(out),
            )
            mismatches += ok != bool(expected_ok[i])
            max_error = max(max_error, abs(out.confidence - expected_conf[i]))
        if mismatches or max_error > 1e-5:
            raise RuntimeError(f"C parity failed: {mismatches} / {max_error}")
        return {
            "frames_checked": len(psd),
            "harmonic_boolean_mismatches": mismatches,
            "max_confidence_absolute_error": max_error,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("test", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    test = args.test
    output = test / "sensitivity-analysis"
    output.mkdir(exist_ok=True)
    files = sorted((test / "capture").glob("*/*.wav"))
    samples = []
    for path in files:
        with wave.open(str(path)) as w:
            if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, RATE):
                raise ValueError("Unsupported audio format")
            samples.append(
                np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(float)
            )
    samples = np.concatenate(samples)
    events = [
        json.loads(line)
        for path in (test / "capture").glob("*/events.jsonl")
        for line in path.read_text().splitlines()
    ]
    start_event = next(x for x in events if x["type"] == "recording")
    base_offset = start_event["sample_offset"]
    # Fit device sample position to receiver UTC using all level records.
    # Network timestamps are approximate; phase scoring excludes 0.75s edges.
    levels = [x for x in events if x["type"] == "level"]
    epochs = np.array(
        [datetime.fromisoformat(x["received_at"]).timestamp() for x in levels]
    )
    positions = np.array([x["sample_offset"] - base_offset for x in levels])
    origin = float(np.median(epochs - positions / RATE))
    rms, bins, snr, h2, h3, psd = features(samples)
    times = origin + (np.arange(len(rms)) * N + N / 2) / RATE
    runs = json.loads((test / "runs.json").read_text())
    masks = [
        (times >= datetime.fromisoformat(run["start"]).timestamp() + 0.75)
        & (times <= datetime.fromisoformat(run["end"]).timestamp() - 0.75)
        for run in runs
    ]
    play_indices = [i for i, x in enumerate(runs) if x["phase"] == "playback"]
    bg_indices = [i for i, x in enumerate(runs) if "background" in x["phase"]]
    quiet = np.logical_or.reduce(
        [mask for i, mask in enumerate(masks) if runs[i]["phase"] != "playback"]
    )
    background = np.logical_or.reduce([masks[i] for i in bg_indices])
    harmonic, conf = confidence(snr, h2, h3)
    parity = verify_c(psd, harmonic, conf, root)
    # Verify the PCM16 replay's levels against live telemetry at exact sample offsets.
    level_errors = []
    for level in levels:
        idx = (level["sample_offset"] - base_offset) // N - 1
        if 0 <= idx < len(rms) and rms[idx] > 0:
            level_errors.append(abs(20 * np.log10(rms[idx]) - level["rms_dbfs"]))
    rows = []
    profiles = {}
    for scale, min_snr, on, gate, sustain in itertools.product(
        (1.0, 0.5, 0.25, 0.1),
        (4.0, 2.0, 1.0),
        (0.3, 0.2, 0.1, 0.05, 0.02),
        (3e-5, 1e-5, 0.0),
        (2,),
    ):
        harm, confidence_values = confidence(
            snr, h2, h3, min_snr, 0.07 * scale, 0.035 * scale
        )
        active, starts, smooth = replay(
            rms, harm, confidence_values, on, on * 0.6, gate, sustain
        )
        run_rows = []
        for index in play_indices:
            mask = masks[index]
            run_rows.append(
                {
                    "clip": runs[index]["clip"],
                    "gain_db": runs[index]["gain_db"],
                    "heard": bool(np.any(active & mask)),
                    "new_alarms": int(np.sum(starts & mask)),
                    "active_seconds": round(float(np.sum(active & mask) * N / RATE), 3),
                }
            )
        row = {
            "harmonic_scale": scale,
            "min_snr": min_snr,
            "confidence_on": on,
            "confidence_off": on * 0.6,
            "rms_gate": gate,
            "sustain_on_frames": sustain,
            "plays_heard": sum(x["heard"] for x in run_rows),
            "quiet_alarm_seconds": round(float(np.sum(active & quiet) * N / RATE), 3),
            "background_alarm_seconds": round(
                float(np.sum(active & background) * N / RATE), 3
            ),
            "quiet_alarm_starts": int(np.sum(starts & quiet)),
            "total_alarm_starts": int(np.sum(starts)),
            "plays": run_rows,
        }
        rows.append(row)
        if (
            scale == 1
            and min_snr == 4
            and gate == 3e-5
            and on in (0.3, 0.2, 0.1, 0.05, 0.02)
        ):
            profiles[str(on)] = {"active": active, "smooth": smooth, "row": row}
    baseline = profiles["0.3"]["row"]
    no_quiet = [r for r in rows if r["quiet_alarm_seconds"] == 0]
    best_clean = max(
        no_quiet, key=lambda r: (r["plays_heard"], -r["total_alarm_starts"])
    )
    best = max(rows, key=lambda r: (r["plays_heard"], -r["quiet_alarm_seconds"]))
    best_harm, best_conf = confidence(
        snr,
        h2,
        h3,
        best["min_snr"],
        0.07 * best["harmonic_scale"],
        0.035 * best["harmonic_scale"],
    )
    candidate_parity = verify_c(
        psd, best_harm, best_conf, root, best["harmonic_scale"], best["min_snr"]
    )
    # Analyze the unplayed source files too, to separate replay-chain issues from thresholds.
    source_rows = []
    for clip in ("mini", "fpv"):
        with wave.open(str(test / f"playback-{clip}--6.wav")) as w:
            src = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(float)
        sr, sb, ss, s2, s3, _ = features(src)
        sh, sc = confidence(ss, s2, s3)
        active, starts, smooth = replay(sr, sh, sc)
        source_rows.append(
            {
                "clip": clip,
                "default_alarm_starts": int(starts.sum()),
                "harmonic_ok_fraction": float(sh.mean()),
                "max_smoothed_confidence": float(smooth.max()),
            }
        )
    diagnostics = {
        "rms_gate_pass_fraction": float(np.mean(rms >= 3e-5)),
        "snr_pass_fraction": float(np.mean(snr >= 4)),
        "h2_pass_fraction": float(np.mean(h2 >= 0.07)),
        "h3_pass_fraction": float(np.mean(h3 >= 0.035)),
        "all_harmonic_checks_pass_fraction": float(harmonic.mean()),
        "default_max_smoothed_confidence": float(profiles["0.3"]["smooth"].max()),
        "median_live_vs_replay_dbfs_error": float(np.median(level_errors)),
        "max_live_vs_replay_dbfs_error": float(max(level_errors)),
        "live_alarms": sum(
            x["type"] == "event" and x.get("event") == "alarm" for x in events
        ),
    }
    result = {
        "test": str(test.resolve()),
        "recorded_seconds": len(samples) / RATE,
        "configurations": len(rows),
        "c_harmonic_parity": parity,
        "relaxed_harmonic_c_parity": candidate_parity,
        "diagnostics": diagnostics,
        "baseline": baseline,
        "confidence_only": [
            profiles[key]["row"] for key in ("0.3", "0.2", "0.1", "0.05", "0.02")
        ],
        "best_without_quiet_alarms": best_clean,
        "best_play_coverage": best,
        "source_clip_checks": source_rows,
        "limits": [
            "Same-session exploratory sweep, not held-out validation.",
            "PCM16 recordings omit the low 8 bits of the original 24-bit microphone data.",
            "NumPy FFT differs in rounding from ESP-DSP; harmonic scan checked against actual C.",
            "EMA/alarm initialized at recording start; original pre-recording state unknown.",
            "Playback labels use approximate receiver UTC; 0.75s excluded from both phase edges.",
            "Alarm occupancy can carry from preceding phases; starts reported separately.",
            "Indoor replay is not a range or real-flight qualification.",
        ],
        "source_sha256": {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (root / "main/audio_processor.c", root / "main/audio_task.c")
        },
    }
    (output / "summary.json").write_text(json.dumps(result, indent=2))
    (output / "sweep.json").write_text(json.dumps(rows, indent=2))
    np.savez_compressed(
        output / "frame_features.npz",
        time_seconds=times - origin,
        rms=rms,
        fundamental_bin=bins,
        snr=snr,
        h2=h2,
        h3=h3,
        confidence=conf,
        harmonic_ok=harmonic,
    )
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(3, 1, figsize=(12, 10), layout="constrained")
    t = times - origin
    for ax in axes[:2]:
        for run in runs:
            if run["phase"] == "playback":
                a = datetime.fromisoformat(run["start"]).timestamp() - origin
                b = datetime.fromisoformat(run["end"]).timestamp() - origin
                ax.axvspan(a, b, color="#2f83c5", alpha=0.12)
                if ax == axes[0]:
                    ax.text(
                        (a + b) / 2,
                        -12,
                        f"{run['clip']}\n{run['gain_db']} dB",
                        ha="center",
                        va="top",
                        fontsize=8,
                    )
        ax.set_xlim(0, t[-1])
        ax.grid(alpha=0.2)
    axes[0].plot(t, 20 * np.log10(np.maximum(rms, 1e-12)), lw=0.7, color="#333333")
    axes[0].set_ylim(-100, -5)
    axes[0].set_ylabel("Windowed level (dBFS)")
    axes[1].plot(
        t,
        profiles["0.3"]["smooth"],
        lw=0.8,
        label="Smoothed confidence, current harmonic rules",
    )
    for threshold, color in (
        (0.3, "#c44e52"),
        (0.2, "#dd8c24"),
        (0.1, "#55a868"),
        (0.05, "#8172b2"),
    ):
        axes[1].axhline(
            threshold,
            ls="--",
            color=color,
            lw=0.8,
            label=f"Alarm threshold {threshold}",
        )
    axes[1].set_ylabel("Confidence")
    axes[1].set_xlabel("Recorded time (s)")
    axes[1].legend(fontsize=8)
    axes[2].scatter(
        [x["quiet_alarm_seconds"] for x in rows],
        [x["plays_heard"] for x in rows],
        s=18,
        alpha=0.25,
    )
    axes[2].set_xlabel("False-alarm occupancy in quiet/background (s)")
    axes[2].set_ylabel("Playback intervals with alarm (of 6)")
    axes[2].set_yticks(range(7))
    axes[2].grid(alpha=0.2)
    fig.suptitle(
        "Indoor replay: sensitivity sweep on recorded microphone audio\nBlue bands = playback; exploratory results, no range qualification"
    )
    fig.savefig(output / "sensitivity.png", dpi=160)
    plt.close(fig)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
