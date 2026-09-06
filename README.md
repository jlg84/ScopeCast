# Astro Weather Report

A small, self-contained script for astrophotographers: it polls several
independent weather sources for your location, builds a consensus forecast
for tonight's astronomical darkness window, and emails you a styled report
twice a day. Runs entirely on your own computer — no hosting, no third-party
cron services, no ongoing cost beyond electricity.

Built after fighting with a hosted (Render + external cron trigger) version
that kept failing due to free-tier cold starts. Running it locally sidesteps
that whole class of problem.

## What it does

- Fetches cloud cover, precipitation, wind, humidity and temperature from
  multiple independent sources and combines them into an ensemble forecast,
  including how much the sources disagree (a rough confidence signal).
- Computes real astronomical twilight (not just sunset/sunrise) so the
  averaging window matches when it's actually dark, and reports total
  hours of darkness.
- Computes moon phase and illumination percentage.
- Emails you a styled HTML report (dark theme, color-coded by cloud cover,
  a small bar chart comparing every source) plus a plain-text fallback.
- Degrades gracefully: if one source fails or is unconfigured, the report
  still sends with whatever succeeded, and tells you which sources are down.

## Data sources

| Source | Cost | Signup |
|---|---|---|
| [Open-Meteo](https://open-meteo.com/) — 9-model ensemble (ECMWF, GFS, ICON, UK Met Office, Météo-France, JMA, GEM, BOM) | Free | None needed |
| [YR.no / MET Norway](https://api.met.no/) | Free | None needed (just identifies itself via your contact email) |
| [7Timer!](http://www.7timer.info/) astronomy feed (seeing & transparency) | Free | None needed |
| [Meteoblue](https://www.meteoblue.com/en/weather-api) | Free tier available | API key required |
| [MetService / MetOcean Point Forecast API](https://data.metservice.com/product/point-forecast-api) | Free tier available | API key required |

Only Open-Meteo, YR.no, and 7Timer! are required — the report works fine
with just those three. Meteoblue and MetService are optional extras; leave
their keys blank during setup to skip them.

## Requirements

- Python 3.9+ (for `zoneinfo`)
- macOS (for the `launchd`-based installer) or Linux (for the cron-based
  installer). Windows works too if you set up Task Scheduler yourself — see
  [Windows](#windows) below.
- A Gmail account to send from, with an
  [App Password](https://myaccount.google.com/apppasswords) (requires
  2-Step Verification turned on). Any SMTP provider would work with a small
  code change, but Gmail is what's wired up out of the box.

## Setup

1. Clone or download this folder.
2. On macOS:
   ```
   bash install.sh
   ```
   On Linux:
   ```
   bash install_linux_cron.sh
   ```
   Either script will walk you through: your location's latitude/longitude,
   a display name for it, your timezone, sender/recipient email, your Gmail
   App Password, and optional Meteoblue/MetService API keys. It writes
   `config.json` and `secrets.json` (git-ignored, never commit these) and
   installs the schedule.
3. Test it:
   ```
   python3 weather_report.py --dry-run    # prints the report, sends nothing
   python3 weather_report.py              # sends a real test email
   ```

### Windows

There's no Windows installer script, but the script itself is plain Python
and works fine there. Create a Scheduled Task that runs
`python weather_report.py` from this folder, twice a day, after doing the
same `config.json`/`secrets.json` setup by hand (copy the `.example.json`
files and fill them in).

## Customizing

- **Location**: edit `latitude`/`longitude`/`location_name`/`timezone` in
  `config.json`.
- **Schedule**: on macOS, edit the `StartCalendarInterval` entries in
  `~/Library/LaunchAgents/local.astro-weather-report.plist`, then:
  ```
  launchctl unload ~/Library/LaunchAgents/local.astro-weather-report.plist
  launchctl load -w ~/Library/LaunchAgents/local.astro-weather-report.plist
  ```
  On Linux, edit with `crontab -e`.
- **Verdict thresholds / colors**: see `_cloud_color()` and the verdict
  logic in `build_report()` in `weather_report.py`.
- **Adding another data source**: follow the pattern of the existing
  `fetch_*` / `collect_*` function pairs, then wire the result into
  `build_report()`'s ensemble and rain-risk logic, and into `render_text()`
  / `render_html()`.

## Files

- `weather_report.py` — the script
- `config.example.json`, `secrets.example.json` — templates; `install.sh`
  generates the real `config.json`/`secrets.json` from your answers
- `install.sh` — macOS setup (launchd)
- `install_linux_cron.sh` — Linux setup (cron)
- `requirements.txt` — the one dependency (`requests`)

## Uninstalling

macOS:
```
launchctl unload ~/Library/LaunchAgents/local.astro-weather-report.plist
rm ~/Library/LaunchAgents/local.astro-weather-report.plist
```

Linux:
```
crontab -l | grep -v "astro-weather-report" | crontab -
```

## Notes on reliability

This only runs while your computer is powered on. On macOS, "Power Nap" /
"Wake for network access" (System Settings → Battery) lets scheduled
`launchd` jobs fire even while asleep in many cases; if the machine is fully
off, that run is simply skipped until the next scheduled time it's on.

## License

MIT — see `LICENSE`. Do whatever you like with it.
