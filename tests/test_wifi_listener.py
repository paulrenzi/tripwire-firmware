import contextlib
import io
import importlib.util
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

spec = importlib.util.spec_from_file_location(
    "wifi_listener", Path(__file__).resolve().parents[1] / "tools/wifi_listener.py"
)
listener = importlib.util.module_from_spec(spec)
spec.loader.exec_module(listener)


class StreamTest(unittest.TestCase):
    def test_fragmented_packet_and_disconnect(self):
        a, b = socket.socketpair()
        with a, b:
            payload = b'{"type":"hello"}'
            packet = listener.HEADER.pack(b"TW01", 1, len(payload)) + payload
            for byte in packet:
                a.sendall(bytes([byte]))
            self.assertEqual(listener.read_packet(b), (1, payload))
            a.close()
            with self.assertRaises(EOFError):
                listener.read_packet(b)

    def test_rotation_and_missing_samples_preserve_timeline(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = listener.Recorder(directory, 1)
            recorder.audio(struct.pack("<Q", 100) + struct.pack("<h", 123) * 15999)
            recorder.audio(struct.pack("<Q", 16101) + struct.pack("<h", -321) * 3)
            recorder.close()
            files = sorted(Path(directory).glob("*.wav"))
            with wave.open(str(files[0])) as wav:
                self.assertEqual(wav.getparams()[:3], (1, 2, 16000))
                self.assertEqual(wav.getnframes(), 16000)
                self.assertEqual(wav.readframes(16000)[-2:], b"\0\0")
            with wave.open(str(files[1])) as wav:
                self.assertEqual(wav.getnframes(), 4)
                self.assertEqual(
                    wav.readframes(4), b"\0\0" + struct.pack("<h", -321) * 3
                )
            events = [
                json.loads(line)
                for line in (Path(directory) / "events.jsonl").read_text().splitlines()
            ]
            self.assertEqual(
                next(e for e in events if e["type"] == "audio_gap")["missing_samples"],
                2,
            )
            self.assertEqual(
                [e["sample_offset"] for e in events if e["type"] == "recording"],
                [100, 16100],
            )

    def test_end_to_end_alert_level_and_audio(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def device():
            with server:
                stream, _ = server.accept()
                with stream:
                    items = [
                        (
                            1,
                            json.dumps(
                                {
                                    "type": "hello",
                                    "device_id": 1,
                                    "boot_id": 42,
                                    "sample_rate": 16000,
                                    "channels": 1,
                                    "sample_width": 2,
                                }
                            ).encode(),
                        ),
                        (
                            1,
                            json.dumps(
                                {"type": "event", "event": "alarm", "confidence": 0.8}
                            ).encode(),
                        ),
                        (
                            1,
                            json.dumps(
                                {
                                    "type": "level",
                                    "alarm": True,
                                    "rms_dbfs": -32,
                                    "laeq_dba": 65,
                                    "audio_drops": 0,
                                    "dma_overflows": 0,
                                }
                            ).encode(),
                        ),
                        (2, struct.pack("<Q", 1024) + struct.pack("<h", 500) * 1024),
                    ]
                    for kind, data in items:
                        wire = listener.HEADER.pack(b"TW01", kind, len(data)) + data
                        # Deliberately split across frame/header boundaries.
                        for i in range(0, len(wire), 7):
                            stream.sendall(wire[i : i + 7])

        thread = threading.Thread(target=device)
        thread.start()
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with contextlib.redirect_stdout(output), self.assertRaises(EOFError):
                listener.capture("127.0.0.1", port, directory, 60)
            self.assertIn("DRONE DETECTION", output.getvalue())
            self.assertIn("DRONE ALERT", output.getvalue())
            self.assertIn("Saving audio: audio-00000.wav", output.getvalue())
            session = next(Path(directory).iterdir())
            events = [
                json.loads(line)
                for line in (session / "events.jsonl").read_text().splitlines()
            ]
            self.assertTrue(any(e.get("event") == "alarm" for e in events))
            self.assertTrue(any(e.get("rms_dbfs") == -32 for e in events))
            with wave.open(str(next(session.glob("*.wav")))) as wav:
                self.assertEqual(wav.getnframes(), 1024)
                self.assertEqual(wav.readframes(1), struct.pack("<h", 500))
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_live_dashboard_alarm_clear_and_terminal_restoration(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaises(RuntimeError):
                with listener.Console(live=True) as console:
                    console.connected("test-session")
                    console.show({"type": "event", "event": "alarm", "confidence": 0.9})
                    self.assertIn(">>> DRONE DETECTION <<<", "\n".join(console.lines()))
                    console.show(
                        {
                            "type": "level",
                            "alarm": True,
                            "rms_dbfs": -32,
                            "laeq_dba": 65,
                            "audio_drops": 12,
                        }
                    )
                    self.assertEqual(console.alarms, 1)
                    with patch.object(
                        listener.time, "monotonic", return_value=console.last_level + 4
                    ):
                        self.assertIn(
                            "WAITING FOR FRESH DETECTOR STATE",
                            "\n".join(console.lines()),
                        )
                    console.status("DISCONNECTED — test")
                    self.assertIn("WAITING FOR CONNECTION", "\n".join(console.lines()))
                    console.connected("second-session")
                    self.assertIsNone(console.alarm)
                    console.show(
                        {
                            "type": "level",
                            "alarm": False,
                            "rms_dbfs": -40,
                            "laeq_dba": 55,
                        }
                    )
                    self.assertIn("LISTENING", "\n".join(console.lines()))
                    raise RuntimeError("simulate interrupted display")
        self.assertIn("\x1b[H", output.getvalue())
        self.assertTrue(output.getvalue().endswith("\x1b[?25h\x1b[?1049l"))

    def test_detection_log_is_immediate_timestamped_and_not_repeated_by_levels(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "detections.log"
            console = listener.Console(live=True, detection_log=path)
            with patch.object(console, "render"):
                console.connected(directory)
                console.show(
                    {
                        "type": "event",
                        "event": "alarm",
                        "confidence": 0.85,
                        "uptime_ms": 1234,
                    }
                )
                first = path.read_text()
                self.assertRegex(
                    first,
                    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2} DRONE DETECTION",
                )
                self.assertIn("confidence=0.850", first)
                for _ in range(3):
                    console.show(
                        {
                            "type": "level",
                            "alarm": True,
                            "rms_dbfs": -30,
                            "laeq_dba": 65,
                        }
                    )
                self.assertEqual(path.read_text(), first)
                console.show({"type": "event", "event": "clear", "confidence": 0.1})
                self.assertEqual(path.read_text(), first)
                console.show(
                    {"type": "level", "alarm": True, "rms_dbfs": -30, "laeq_dba": 65}
                )
                console.show({"type": "event", "event": "alarm", "confidence": 0.9})
                self.assertEqual(len(path.read_text().splitlines()), 2)
                self.assertEqual(console.alarms, 2)
                self.assertEqual(len(console.detections), 2)
                self.assertIn("DRONE DETECTION", "\n".join(console.lines()))

    def test_bad_header_rejected(self):
        a, b = socket.socketpair()
        with a, b:
            a.sendall(listener.HEADER.pack(b"BAD!", 1, 0))
            with self.assertRaises(ValueError):
                listener.read_packet(b)


if __name__ == "__main__":
    unittest.main()
