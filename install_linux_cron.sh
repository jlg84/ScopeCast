#!/bin/bash
# Interactive setup for Linux (uses cron for scheduling).
# For macOS, use install.sh instead.
set -e
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="$(command -v python3 || true)"

if [ -z "$PYTHON_BIN" ]; then
  echo "Could not find python3 on your PATH. Install it with your package manager and re-run."
  exit 1
fi
echo "Using python3 at: $PYTHON_BIN"

echo "Checking for the 'requests' package..."
if ! "$PYTHON_BIN" -c "import requests" 2>/dev/null; then
  echo "Installing requests..."
  "$PYTHON_BIN" -m pip install --user requests
fi

CONFIG_PATH="$PROJECT_DIR/config.json"
SECRETS_PATH="$PROJECT_DIR/secrets.json"

if [ -f "$CONFIG_PATH" ]; then
  echo "config.json already exists — skipping location/email setup."
else
  echo ""
  echo "=== Location & email setup ==="
  read -p "Latitude (decimal degrees): " LAT
  read -p "Longitude (decimal degrees): " LON
  read -p "A short name for this location: " LOC_NAME
  read -p "Timezone (IANA name, e.g. Pacific/Auckland, America/Denver, Europe/London): " TZ_NAME
  read -p "Email address to send the report FROM (needs a Gmail App Password): " SENDER_EMAIL
  read -p "Email address to send the report TO [default: same as above]: " RECIPIENT_EMAIL
  RECIPIENT_EMAIL="${RECIPIENT_EMAIL:-$SENDER_EMAIL}"

  "$PYTHON_BIN" - "$CONFIG_PATH" "$LAT" "$LON" "$LOC_NAME" "$TZ_NAME" "$SENDER_EMAIL" "$RECIPIENT_EMAIL" << 'PYEOF'
import json, sys
path, lat, lon, loc_name, tz_name, sender, recipient = sys.argv[1:8]
config = {
    "latitude": float(lat), "longitude": float(lon), "location_name": loc_name,
    "timezone": tz_name, "sender": sender, "recipient": recipient, "contact_email": sender,
}
with open(path, "w") as f:
    json.dump(config, f, indent=2)
print("Wrote", path)
PYEOF
fi

if [ -f "$SECRETS_PATH" ]; then
  echo "secrets.json already exists — skipping key/password setup."
else
  echo ""
  echo "=== Gmail App Password ==="
  echo "Create one at https://myaccount.google.com/apppasswords (needs 2-Step Verification on)."
  read -s -p "Paste the Gmail App Password here (input hidden): " GMAIL_PW
  echo ""
  read -p "Meteoblue API key (optional, blank to skip): " MB_KEY
  read -p "MetService/MetOcean Point Forecast API key (optional, blank to skip): " MS_KEY

  "$PYTHON_BIN" - "$SECRETS_PATH" "$GMAIL_PW" "$MB_KEY" "$MS_KEY" << 'PYEOF'
import json, sys
path, gmail_pw, mb_key, ms_key = sys.argv[1:5]
secrets = {"gmail_app_password": gmail_pw, "meteoblue_api_key": mb_key, "metservice_api_key": ms_key}
with open(path, "w") as f:
    json.dump(secrets, f, indent=2)
PYEOF
  chmod 600 "$SECRETS_PATH"
  echo "Saved secrets.json (permissions locked to your user only)."
fi

echo ""
echo "=== Schedule ==="
read -p "Hour to send the morning report (0-23) [default 9]: " HOUR1
HOUR1="${HOUR1:-9}"
read -p "Hour to send the afternoon/evening report (0-23) [default 16]: " HOUR2
HOUR2="${HOUR2:-16}"

CRON_MARKER="# astro-weather-report ($PROJECT_DIR)"
CRON_LINE1="0 $HOUR1 * * * cd \"$PROJECT_DIR\" && \"$PYTHON_BIN\" weather_report.py >> \"$PROJECT_DIR/cron.log\" 2>&1 $CRON_MARKER"
CRON_LINE2="0 $HOUR2 * * * cd \"$PROJECT_DIR\" && \"$PYTHON_BIN\" weather_report.py >> \"$PROJECT_DIR/cron.log\" 2>&1 $CRON_MARKER"

( crontab -l 2>/dev/null | grep -v "$CRON_MARKER" ; echo "$CRON_LINE1" ; echo "$CRON_LINE2" ) | crontab -

echo ""
echo "Installed two cron entries (at ${HOUR1}:00 and ${HOUR2}:00 daily, system time zone)."
echo "View them with: crontab -l"
echo "Remove them later with: crontab -l | grep -v \"astro-weather-report ($PROJECT_DIR)\" | crontab -"
echo ""
echo "Test it right now with:"
echo "  cd \"$PROJECT_DIR\" && \"$PYTHON_BIN\" weather_report.py --dry-run"
