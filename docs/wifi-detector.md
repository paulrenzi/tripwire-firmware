# Local Wi-Fi detector and recordings

The `sdkconfig.detector_xiao_wifi` profile uses the XIAO ESP32-S3 with an
external INMP441 / ICS-43434 microphone. No LoRa carrier or gateway is needed.
It replaces the detector's LoRa transport; the existing LoRa profiles remain
available separately.

| Microphone | XIAO | GPIO |
|---|---|---|
| SCK | D3 | 4 |
| WS | D4 | 5 |
| SD | D5 | 6 |
| L/R | GND | left channel |
| VDD | 3V3 | |
| GND | GND | |

Disconnect power before wiring. Firmware reads stereo I2S and selects the
left slot. Recordings are signed PCM16 little endian, mono, 16 kHz, without
playback gain. Quiet recordings may need gain when played back. Sound levels
in dBFS are digital levels; dBA / SPL figures are estimates, not calibrated
measurements.

## Build and flash

With ESP-IDF 6.0 activated:

```sh
idf.py -B build_detector_xiao_wifi \
  -DSDKCONFIG=build_detector_xiao_wifi/sdkconfig \
  -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.detector_xiao_wifi" \
  set-target esp32s3
idf.py -B build_detector_xiao_wifi \
  -DSDKCONFIG=build_detector_xiao_wifi/sdkconfig build
idf.py -B build_detector_xiao_wifi \
  -DSDKCONFIG=build_detector_xiao_wifi/sdkconfig -p PORT flash
```

Native USB sometimes remains in download mode after a hard reset. Use
esptool's `--after watchdog-reset` when flashing, or reconnect power. The USB
serial device may change between `/dev/ttyACM0` and `/dev/ttyACM1`; identify
it through `/dev/serial/by-id/`.

## Connect and record

With no saved detector Wi-Fi SSID, the chip creates:

- Network: **Tripwire-XIAO** (2.4 GHz)
- Password: **tripwire-local**
- Address: **192.168.4.1**, TCP port **8765**

Join that network from the computer. On Linux, mark its connection as
`ipv4.never-default yes` and `ipv6.never-default yes` if Ethernet is also
connected, so it does not replace the Internet route.

Then run from the repository root (Python 3, no additional packages):

```sh
./listen.sh
```

The launcher runs in the foreground with a live terminal dashboard that updates
in place, with sound levels sent every 64 ms (about 16 updates per second).
The dashboard displays the latest sound levels without smoothing, refreshing
up to four times per second (every 250 ms). Alarm transitions display immediately.
Recordings and file logs retain the full incoming data rate. Serial diagnostics
keep their one-second average. It shows the current detection state, sound-level bar, latest alert,
connection status, recording filename, received audio duration, and capture
health. There is no background recorder or automatic startup; recording happens
only while the script is running. Ctrl+C restores the terminal and finalizes the
active WAV file. On reconnect the dashboard waits for fresh detector state.

Each detection immediately appends a timestamped entry (local time with UTC
offset, confidence when available, device uptime, and current recording) to
`recordings/detections.log`. The dashboard retains the three latest detection
entries. Repeated active-state updates do not create duplicate detections.
An already-active alarm discovered on connection is labeled `source=device state`.

Detailed diagnostic logs go to `recordings/monitor-<UTC timestamp>.log`.
Each recording session also has its structured `events.jsonl` file. Neither log
scrolls through the live screen. Output automatically uses plain lines when
redirected; `--plain` explicitly selects that mode.

Preview a simulated alarm and clear without connecting or recording:

```sh
./listen.sh --demo
```

For a bounded recording test:

```sh
./listen.sh --duration 30
```

Only one receiver can stream at a time. WAV files rotate every 60 seconds
under `recordings/<UTC session>/`. `events.jsonl` stores device boot ID,
alerts, confidence, levels, device uptime, sample offsets, receiver UTC times,
and per-file starting offsets. Each network reconnect creates a new session.
Files are recorded continuously while the receiver is connected, so audio
before and after an alert is retained. This uses about 115 MB/hour; stop the
listener when finished. There is no automatic disk deletion or recording on
the XIAO itself. An interrupted active WAV may need header repair after a
hard process kill; completed segments are finalized.

TCP preserves packet order. The audio task uses a bounded nonblocking queue;
a stalled receiver cannot block microphone processing. Dropped audio blocks
carry sample-offset gaps: the receiver logs an `audio_gap` and inserts silence
(up to 30 seconds). Longer gaps create a new recording session. I2S DMA
overflow counters are included in level records; DMA overruns indicate capture
loss whose exact timing is not reconstructed. Treat affected clips as
incomplete. A current alarm state is included in level updates so reconnects
do not imply a clear state. Alerts while disconnected are not replayed.

This mode reads continuously instead of using the LoRa detector's 100 ms
pause after each 1024-sample frame. The detection thresholds are unchanged,
so frame-count debounce and confidence smoothing respond faster in wall-clock
time. Detection performance has to be evaluated with recorded examples.

The AP has a published bench password and the TCP endpoint has no separate
authentication or encryption. Use it locally; do not expose port 8765 to the
Internet. Anyone joined to this AP can receive microphone audio.

## Optional: join an existing LAN

Provision `wifi_ssid` and `wifi_pass` in the `detector_wifi` NVS namespace
through the serial console, then reboot. `tools/provision_wifi.py` uses a
masked password prompt and suppresses echoed serial credentials (requires
`pyserial` and permission to access the USB serial device):

```sh
python3 tools/provision_wifi.py --port /dev/ttyACM0 --ssid "Your network"
```

The startup logs print the DHCP address. Use it instead of `192.168.4.1` in
the listener command. The network must offer 2.4 GHz Wi-Fi and allow connections
between clients. To return to AP mode, send `set wifi_ssid ""` and `reboot`
through the serial console. Credentials are stored on the chip, not in source.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

## Indoor Bluetooth replay check

`tools/speaker_test.py` runs a short local adaptation of the
[Saturday speaker test](https://paulrenzi.github.io/tripwire/weekend-test.html).
It captures directly from the XIAO over Wi-Fi and plays through an explicitly
selected PulseAudio sink. Close `listen.sh` first so it releases the stream.

The decoded clips used by the initial run are in
`recordings/speaker-test-assets/`. To prepare them on another checkout, download
`mini.mp3` and `fpv.mp3` from the page's
`https://paulrenzi.github.io/tripwire/tools/weekend/sounds/` directory, then decode
them to mono PCM16 WAV at 16 kHz (filenames `mini.wav` / `fpv.wav`). The runner
requires Python with NumPy and `paplay`; GStreamer can decode the MP3 assets.

```sh
pactl list short sinks
python3 tools/speaker_test.py --sink YOUR_BLUETOOTH_SINK
```

It records 10 seconds of initial background, 12-second Mini/FPV plays at
relative digital gains of −24, −12 and −6 dB with 6-second quiet gaps, and
20 seconds of final background. This is approximately 2½ minutes. The speaker's
system/hardware volume stays fixed. The runner uses the page's peak
normalization, loop crossfade, and fade-in/fade-out approach.

Each test folder under `recordings/speaker-tests/` contains:

- `test.json`: source URLs/hashes, speaker sink, and qualification limits.
- `runs.json`: actual computer playback start/end times and relative gains.
- `results.json`: alarm transitions, active-alarm samples, and median digital
  sound levels for each phase.
- `capture/<session>/`: continuous WAV segments and timestamped detector events.
- `monitor.log`, and `detections.log` if a detection occurs.
- `playback-*.wav`: exact prepared audio sent to the speaker.

Recordings run only during this foreground test command. Ctrl+C stops it and
finalizes the active recording. Bluetooth playback latency has not been
calibrated; phase labels use computer playback/receive timestamps, so boundary
alignment is approximate. An alarm active at the start of a play can be a
carryover from the previous phase; distinguish active samples from new alarm
transitions in the results.

This is an uncalibrated indoor replay check, not the outdoor 16-foot geometry,
distance ladder, three-play reliability test, or radio walk described on the
page. There are no emulated-distance or range-pass claims from this run.

## Analysis, export, and findings

The optional tools need dependencies from `tools/requirements.txt`; the live
listener itself needs only Python's standard library. Offline analysis also
needs a host C compiler to compare its harmonic scan with the firmware C:

```sh
python3 -m pip install -r tools/requirements.txt
python3 tools/analyze_sensitivity.py recordings/speaker-tests/YOUR_TEST
python3 tools/export_weekend_session.py recordings/speaker-tests/YOUR_TEST \
  --speaker-distance-ft 12
```

The exporter writes `weekend-session.json` locally, deriving a session ID from
the test's UTC start time unless `--session` is supplied. `--gateway` names the
API namespace; it does not imply a LoRa gateway was present. Physical speaker
distance is kept separate from the logger's emulated-drone-distance field.
The current exporter handles no-alarm captures and rejects captures with live
alarms rather than emitting unverified alarm-duration summaries. It never
uploads automatically. The external Saturday-session API stores public test
metadata/events, not WAV recordings; review the payload before submitting it.

The [2026-10-24 indoor findings](experiments/indoor-replay-2026-10-24.md) document
capture quality, the live zero-alarm result, and the false-alarm tradeoffs of
an offline sensitivity sweep. Raw room audio and downloaded source clips are
excluded from Git.
