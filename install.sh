#!/bin/bash
# Interactive setup for macOS (uses launchd for scheduling).
# For Linux, use install_linux_cron.sh instead.
set -e
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="$(command -v python3 || true)"

if [ -z "$PYTHON_BIN" ]; then
  echo "Could not find python3 on your PATH. Install Python 3 first (python.org or Homebrew) and re-run."
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
  echo ""
  echo "config.json already exists — skipping location/email setup."
  echo "(Delete it and re-run this script if you want to redo that step.)"
else
  echo ""
  echo "=== Location & email setup ==="
  read -p "Latitude (decimal degrees, e.g. -41.29 for Wellington NZ): " LAT
  read -p "Longitude (decimal degrees, e.g. 174.78): " LON
  read -p "A short name for this location (e.g. 'My backyard, Wellington'): " LOC_NAME
  read -p "Timezone (IANA name, e.g. Pacific/Auckland, America/Denver, Europe/London): " TZ_NAME
  read -p "Email address to send the report FROM (needs a Gmail App Password — see below): " SENDER_EMAIL
  read -p "Email address to send the report TO [default: same as above]: " RECIPIENT_EMAIL
  RECIPIENT_EMAIL="${RECIPIENT_EMAIL:-$SENDER_EMAIL}"

  "$PYTHON_BIN" - "$CONFIG_PATH" "$LAT" "$LON" "$LOC_NAME" "$TZ_NAME" "$SENDER_EMAIL" "$RECIPIENT_EMAIL" << 'PYEOF'
import json, sys
path, lat, lon, loc_name, tz_name, sender, recipient = sys.argv[1:8]
config = {
    "latitude": float(lat),
    "longitude": float(lon),
    "location_name": loc_name,
    "timezone": tz_name,
    "sender": sender,
    "recipient": recipient,
    "contact_email": sender,
}
with open(path, "w") as f:
    json.dump(config, f, indent=2)
print("Wrote", path)
PYEOF
fi

if [ -f "$SECRETS_PATH" ]; then
  echo ""
  echo "secrets.json already exists — skipping key/password setup."
  echo "(Edit it directly, or delete it and re-run this script to redo that step.)"
else
  echo ""
  echo "=== Gmail App Password ==="
  echo "This script sends mail via Gmail SMTP using an App Password (not your"
  echo "normal Gmail password). To create one:"
  echo "  1. Turn on 2-Step Verification on the sending Google account"
  echo "  2. Go to https://myaccount.google.com/apppasswords"
  echo "  3. Create one for 'Mail' and copy the 16-character code"
  echo ""
  read -s -p "Paste the Gmail App Password here (input hidden): " GMAIL_PW
  echo ""

  echo ""
  echo "=== Optional extra data sources ==="
  echo "These are optional. Leave blank to skip — the report works fine with"
  echo "just the free sources (Open-Meteo, YR.no, 7Timer!)."
  echo ""
  read -p "Meteoblue API key (https://www.meteoblue.com/en/weather-api), or leave blank: " MB_KEY
  read -p "MetService/MetOcean Point Forecast API key (https://data.metservice.com/product/point-forecast-api), or leave blank: " MS_KEY

  "$PYTHON_BIN" - "$SECRETS_PATH" "$GMAIL_PW" "$MB_KEY" "$MS_KEY" << 'PYEOF'
import json, sys
path, gmail_pw, mb_key, ms_key = sys.argv[1:5]
secrets = {
    "gmail_app_password": gmail_pw,
    "meteoblue_api_key": mb_key,
    "metservice_api_key": ms_key,
}
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

PLIST_LABEL="local.astro-weather-report"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

cat > "$PLIST_PATH" << PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON_BIN}</string>
        <string>${PROJECT_DIR}/weather_report.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${PROJECT_DIR}</string>
    <key>StartCalendarInterval</key>
    <array>
        <dict>
            <key>Hour</key>
            <integer>${HOUR1}</integer>
            <key>Minute</key>
            <integer>0</integer>
        </dict>
        <dict>
            <key>Hour</key>
            <integer>${HOUR2}</integer>
            <key>Minute</key>
            <integer>0</integer>
        </dict>
    </array>
    <key>StandardOutPath</key>
    <string>${PROJECT_DIR}/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>${PROJECT_DIR}/launchd.err.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
PLISTEOF

echo "Wrote $PLIST_PATH"
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load -w "$PLIST_PATH"

echo ""
echo "Installed. The report will run automatically at ${HOUR1}:00 and ${HOUR2}:00"
echo "daily (your Mac's local time zone). It only runs while the Mac is on."
echo ""
echo "Test it right now with:"
echo "  cd \"$PROJECT_DIR\" && \"$PYTHON_BIN\" weather_report.py --dry-run"
echo "(add --dry-run to just print the report; drop it to actually send a test email)"
