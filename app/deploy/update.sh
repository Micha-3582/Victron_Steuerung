#!/usr/bin/env bash
#
# Manuelles Update (Alternative zum Update-Knopf in der App).
# Holt den neuesten Code von GitHub und startet den Dienst neu.
#
set -euo pipefail
DIR="$HOME/victron-steuerung"
SERVICE="victron-steuerung"

git -C "$DIR" pull --ff-only
"$DIR/app/.venv/bin/pip" install --quiet -r "$DIR/app/requirements.txt"
sudo systemctl restart "$SERVICE"
echo "Aktualisiert und neu gestartet."
