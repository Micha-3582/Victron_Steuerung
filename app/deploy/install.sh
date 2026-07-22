#!/usr/bin/env bash
#
# Installations-Skript für die Victron Standalone Steuerung (Raspberry Pi / Debian).
# Klont das öffentliche GitHub-Repo, richtet Python-Umgebung + systemd-Dienst ein.
#
# Nutzung:
#   bash install.sh https://github.com/Micha-3582/Victron_Steuerung.git
#
set -euo pipefail

REPO="${1:-}"
if [ -z "$REPO" ]; then
  echo "Bitte GitHub-Repo-URL angeben:"
  echo "  bash install.sh https://github.com/Micha-3582/Victron_Steuerung.git"
  exit 1
fi

DIR="$HOME/victron-steuerung"
SERVICE="victron-steuerung"
USER_NAME="$(whoami)"

echo ">> Installiere Abhängigkeiten (git, python3-venv) ..."
sudo apt-get update -qq
sudo apt-get install -y git python3-venv python3-pip >/dev/null

echo ">> Hole Code von GitHub ..."
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
else
  git clone "$REPO" "$DIR"
fi

APP="$DIR/app"
echo ">> Richte Python-Umgebung ein ..."
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install --quiet --upgrade pip
"$APP/.venv/bin/pip" install --quiet -r "$APP/requirements.txt"

echo ">> Erstelle systemd-Dienst ..."
sudo tee "/etc/systemd/system/${SERVICE}.service" >/dev/null <<EOF
[Unit]
Description=Victron Standalone Steuerung
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USER_NAME}
WorkingDirectory=${APP}
ExecStart=${APP}/.venv/bin/python webapp.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now "${SERVICE}"

IP="$(hostname -I | awk '{print $1}')"
echo ""
echo "============================================================"
echo " Fertig! Die App läuft als Dienst '${SERVICE}'."
echo " Öffne im Browser:   http://${IP}:5005"
echo " Der Einrichtungsassistent führt dich durch die Konfiguration."
echo ""
echo " Nützliche Befehle:"
echo "   sudo systemctl status ${SERVICE}     # Status"
echo "   sudo systemctl restart ${SERVICE}    # Neustart"
echo "   journalctl -u ${SERVICE} -f          # Live-Log"
echo "============================================================"
