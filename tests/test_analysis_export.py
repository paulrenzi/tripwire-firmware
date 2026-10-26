import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


analysis = load("analyze_sensitivity")
exporter = load("export_weekend_session")


class AnalysisTest(unittest.TestCase):
    def test_harmonic_scan_matches_c_for_harmonic_and_nonharmonic_frames(self):
        t = np.arange(1024) / 16000
        tone = (
            np.sin(2 * np.pi * 500 * t)
            + 0.5 * np.sin(2 * np.pi * 1000 * t)
            + 0.3 * np.sin(2 * np.pi * 1500 * t)
        ) * 5000
        pure = np.sin(2 * np.pi * 500 * t) * 5000
        noise = np.random.default_rng(3).normal(0, 500, 1024)
        samples = np.concatenate([tone, pure, noise, np.zeros(1024)])
        rms, bins, snr, h2, h3, psd = analysis.features(samples)
        ok, conf = analysis.confidence(snr, h2, h3)
        self.assertTrue(ok[0])
        self.assertFalse(ok[1])
        parity = analysis.verify_c(psd, ok, conf, ROOT)
        self.assertEqual(parity["harmonic_boolean_mismatches"], 0)
        relaxed, score = analysis.confidence(snr, h2, h3, 4, 0.035, 0.0175)
        analysis.verify_c(psd, relaxed, score, ROOT, 0.5, 4.0)

    def test_alarm_requires_sustain_and_clears_after_eight_bad_frames(self):
        rms = np.full(16, 0.01)
        harmonic = np.array([True] * 4 + [False] * 12)
        conf = np.array([1.0] * 4 + [0.0] * 12)
        active, starts, ema = analysis.replay(rms, harmonic, conf)
        self.assertEqual(np.flatnonzero(starts).tolist(), [2])
        self.assertTrue(active[10])
        self.assertFalse(active[11])
        self.assertFalse(active[-1])


class ExportTest(unittest.TestCase):
    def make_test(self, path):
        (path / "capture/session").mkdir(parents=True)
        (path / "test.json").write_text(
            json.dumps({"started_at": "2026-10-24T18:27:25Z", "sources": {}})
        )
        (path / "quality.json").write_text("{}")
        (path / "runs.json").write_text(
            json.dumps(
                [
                    {
                        "phase": "playback",
                        "clip": "mini",
                        "gain_db": -12,
                        "start": "2026-10-24T18:27:25+00:00",
                        "end": "2026-10-24T18:27:37+00:00",
                    }
                ]
            )
        )
        events = [
            {
                "received_at": "2026-10-24T18:27:26+00:00",
                "type": "level",
                "laeq_dba": 60.0,
                "lzeq_db": 62.0,
                "rms_dbfs": -45.0,
                "alarm": False,
            }
        ]
        (path / "capture/session/events.jsonl").write_text(
            "\n".join(map(json.dumps, events))
        )

    def test_physical_distance_and_measured_levels_do_not_become_calibration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            self.make_test(path)
            payload = exporter.export(path, "xiao", "S-test", 12)
            run = payload["runs"][0]
            self.assertIsNone(run["distance_ft"])
            self.assertEqual(run["speaker_distance_ft"], 12)
            self.assertIsNone(run["cal_dba"])
            self.assertIsNone(run["mic_dba"])
            self.assertEqual(run["det_dbfs"], -45.0)
            self.assertEqual(run["heard"], 0)
            self.assertEqual(payload["events"][1]["source"], "detector")
            self.assertFalse(payload["metadata"]["recordings_uploaded"])
            self.assertFalse(payload["metadata"]["offline_sensitivity_included"])

    def test_alarm_captures_are_rejected_before_unverified_summaries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            self.make_test(path)
            events = path / "capture/session/events.jsonl"
            events.write_text(
                events.read_text().replace('"alarm": false', '"alarm": true')
            )
            with self.assertRaisesRegex(ValueError, "live alarms"):
                exporter.export(path, "xiao", "S-test", 12)


if __name__ == "__main__":
    unittest.main()
