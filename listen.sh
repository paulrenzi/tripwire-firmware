#!/usr/bin/env bash
# Foreground monitor: records only for the lifetime of this command.
set -euo pipefail
tripwire_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$tripwire_root"
exec python3 -u tools/wifi_listener.py "$@"
