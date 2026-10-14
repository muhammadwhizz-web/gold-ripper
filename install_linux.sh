#!/usr/bin/env bash
# GOLD RIPPER :: Linux installer (Ubuntu/Debian/Arch/Fedora)
# - venv + deps + trained calibration check
# - systemd USER services (ripper + watchdog), Restart=always, boot persistence
set -euo pipefail
cd "$(dirname "$0")"

echo "── GOLD RIPPER Linux installer ──────────────────────────"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[!] python3 missing. Install Python 3.10+ first."
  exit 1
fi

PYVER=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
echo "[+] python $PYVER"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip -q
pip install -q -r requirements.txt
echo "[+] dependencies installed"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "[+] created .env from template - EDIT IT (Bitget keys for LIVE)"
fi

# fetch history + ensure calibration exists
if [ ! -f data/history/gold_1h.csv ]; then
  echo "[+] fetching gold history for local training..."
  python data/fetch_history.py || true
fi
if [ ! -f data/calibration.json ]; then
  echo "[+] no calibration.json - training locally (this can take ~5 min)..."
  python train.py || echo "[!] training failed - bot will run on safe defaults"
fi

# systemd user services
SYSTEMD_DIR="${HOME}/.config/systemd/user"
mkdir -p "$SYSTEMD_DIR"

cat > "$SYSTEMD_DIR/gold-ripper.service" <<EOF
[Unit]
Description=GOLD RIPPER - 24/7 gold hunter
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$(pwd)
ExecStart=$(pwd)/.venv/bin/python $(pwd)/ripper.py
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
EOF

cat > "$SYSTEMD_DIR/gold-ripper-watchdog.service" <<EOF
[Unit]
Description=GOLD RIPPER watchdog
After=gold-ripper.service

[Service]
Type=simple
WorkingDirectory=$(pwd)
ExecStart=$(pwd)/.venv/bin/python $(pwd)/watchdog.py
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now gold-ripper.service gold-ripper-watchdog.service
loginctl enable-linger "$USER" 2>/dev/null || true

echo ""
echo "[✓] GOLD RIPPER installed and RUNNING (paper mode by default)"
echo "    status : systemctl --user status gold-ripper"
echo "    logs   : journalctl --user -u gold-ripper -f"
echo "    rails  : python ripper.py --status"
echo "    LIVE   : edit .env -> RIPPER_MODE=LIVE + Bitget keys, then"
echo "             systemctl --user restart gold-ripper"
