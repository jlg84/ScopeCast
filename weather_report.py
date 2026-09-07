#!/usr/bin/env python3
"""
APD Local Weather Report
Polls multiple free weather sources (Open-Meteo multi-model, MET Norway/YR.no,
7Timer! astro feed) plus optionally Meteoblue, builds a consensus forecast for
tonight's astrophotography conditions at a fixed location, and emails a summary.

Run manually:
    python3 weather_report.py --dry-run     # print report, don't send email
    python3 weather_report.py               # build report and send email
"""
import argparse
import json
import math
import statistics
import smtplib
import subprocess
import sys
import traceback
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
SECRETS_PATH = SCRIPT_DIR / "secrets.json"
LOG_PATH = SCRIPT_DIR / "weather_report.log"

OPEN_METEO_MODELS = [
    "best_match",
    "ecmwf_ifs025",
    "gfs_seamless",
    "icon_seamless",
    "ukmo_seamless",
    "meteofrance_seamless",
    "jma_seamless",
    "gem_seamless",
    "bom_access_global",
]


def log(msg):
    line = f"{datetime.now().isoformat()} {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def get_secret(name):
    """Look up a secret. Tries a local secrets.json first, then falls back to
    macOS Keychain (useful if you migrate the value there later)."""
    try:
        if SECRETS_PATH.exists():
            with open(SECRETS_PATH) as f:
                data = json.load(f)
            if data.get(name):
                return data[name]
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-a", name, "-s", "apd-weather", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------
# Moon phase (simple approximation, good enough for planning purposes)
# --------------------------------------------------------------------------
def moon_phase_info(dt_utc):
    ref = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
    synodic = 29.530588853
    days = (dt_utc - ref).total_seconds() / 86400.0
    phase = (days % synodic) / synodic
    illumination = (1 - math.cos(2 * math.pi * phase)) / 2 * 100
    if phase < 0.033 or phase > 0.967:
        name = "New Moon"
    elif phase < 0.25:
        name = "Waxing Crescent"
    elif phase < 0.283:
        name = "First Quarter"
    elif phase < 0.467:
        name = "Waxing Gibbous"
    elif phase < 0.533:
        name = "Full Moon"
    elif phase < 0.717:
        name = "Waning Gibbous"
    elif phase < 0.75:
        name = "Last Quarter"
    else:
        name = "Waning Crescent"
    return illumination, name


# --------------------------------------------------------------------------
# Data sources
# --------------------------------------------------------------------------
def fetch_open_meteo(lat, lon, tz):
    base = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "cloudcover,precipitation_probability,temperature_2m,"
                   "relative_humidity_2m,windspeed_10m,windgusts_10m,dewpoint_2m",
        "daily": "sunrise,sunset",
        "timezone": tz,
        "forecast_days": 2,
        "models": ",".join(OPEN_METEO_MODELS),
    }
    r = requests.get(base, params=params, timeout=25)
    if r.status_code != 200:
        log(f"Open-Meteo multi-model request failed ({r.status_code}), retrying with best_match only")
        params["models"] = "best_match"
        r = requests.get(base, params=params, timeout=25)
    r.raise_for_status()
    data = r.json()
    data["_models_requested"] = params["models"].split(",")
    return data


def fetch_yr(lat, lon, contact_email):
    url = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
    headers = {"User-Agent": f"APD-Weather-Report/1.0 (contact: {contact_email})"}
    params = {"lat": lat, "lon": lon}
    r = requests.get(url, headers=headers, params=params, timeout=25)
    r.raise_for_status()
    return r.json()


def fetch_7timer(lat, lon):
    url = "http://www.7timer.info/bin/astro.php"
    params = {"lon": lon, "lat": lat, "ac": 0, "unit": "metric", "output": "json", "tzshift": 0}
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json()


def fetch_meteoblue(lat, lon, api_key):
    if not api_key:
        return None
    # basic-1h has precipitation_probability but no cloud cover; clouds-1h has
    # totalcloudcover. Combine both packages in one request.
    url = "https://my.meteoblue.com/packages/basic-1h_clouds-1h"
    params = {"apikey": api_key, "lat": lat, "lon": lon, "format": "json"}
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json()


def fetch_metservice(lat, lon, api_key):
    if not api_key:
        return None
    now = datetime.now(timezone.utc)
    to = now + timedelta(hours=48)
    url = "https://forecast-v2.metoceanapi.com/point/time"
    headers = {"x-api-key": api_key}
    params = {
        "lat": lat,
        "lon": lon,
        "variables": "cloud.cover,precipitation.rate",
        "interval": "1h",
        "from": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": to.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    r = requests.get(url, headers=headers, params=params, timeout=25)
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------
# Aggregation helpers
# --------------------------------------------------------------------------
def parse_iso_local(s, tz):
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _find_daily_key(daily, prefix):
    """Open-Meteo sometimes suffixes daily variables with the model name
    (e.g. 'sunset_best_match') when multiple models are requested, and
    sometimes returns the plain name ('sunset'). Handle both."""
    if prefix in daily:
        return prefix
    candidates = [k for k in daily.keys() if k.startswith(prefix)]
    if not candidates:
        raise KeyError(f"No '{prefix}*' key in daily response: {list(daily.keys())}")
    for c in candidates:
        if "best_match" in c:
            return c
    return candidates[0]


def _hourly_series(hourly, base_key, preferred_model="best_match"):
    """Same suffixing issue as _find_daily_key, but for hourly variables:
    when multiple models are requested, Open-Meteo suffixes EVERY hourly
    variable with the model name (e.g. 'windspeed_10m_best_match'), not
    just cloud cover. For single-value (non-ensemble) variables like temp,
    humidity, wind and gusts, we just want one representative series, so
    prefer the plain key, then the preferred model's suffixed key, then
    whatever suffixed variant is available."""
    if base_key in hourly:
        return hourly[base_key]
    preferred_key = f"{base_key}_{preferred_model}"
    if preferred_key in hourly:
        return hourly[preferred_key]
    candidates = [k for k in hourly.keys() if k.startswith(base_key + "_")]
    if candidates:
        return hourly[candidates[0]]
    return None


def night_window(open_meteo_data, tz):
    daily = open_meteo_data["daily"]
    sunset_key = _find_daily_key(daily, "sunset")
    sunrise_key = _find_daily_key(daily, "sunrise")
    sunset_today = parse_iso_local(daily[sunset_key][0], tz)
    sunrise_tomorrow = parse_iso_local(daily[sunrise_key][1], tz)
    return sunset_today, sunrise_tomorrow


def fetch_twilight(lat, lon, date_str):
    url = "https://api.sunrise-sunset.org/json"
    params = {"lat": lat, "lng": lon, "date": date_str, "formatted": 0}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "OK":
        raise RuntimeError(f"sunrise-sunset.org status: {data.get('status')}")
    return data["results"]


def astro_dark_window(lat, lon, tz, now_local):
    # sunrise-sunset.org slices by UTC calendar day, so for a location far
    # ahead of UTC (like NZ), today's local evening dusk comes from querying
    # *today's* local date, while tomorrow's local morning dawn comes from
    # querying *tomorrow's* local date.
    today_str = now_local.strftime("%Y-%m-%d")
    tomorrow_str = (now_local + timedelta(days=1)).strftime("%Y-%m-%d")
    today_res = fetch_twilight(lat, lon, today_str)
    tomorrow_res = fetch_twilight(lat, lon, tomorrow_str)
    dusk = datetime.fromisoformat(today_res["astronomical_twilight_end"]).astimezone(tz)
    dawn = datetime.fromisoformat(tomorrow_res["astronomical_twilight_begin"]).astimezone(tz)
    return dusk, dawn


def collect_open_meteo_series(data, tz, start, end):
    hourly = data["hourly"]
    times = hourly["time"]
    models = data.get("_models_requested", ["best_match"])
    per_model_cloud = {m: [] for m in models}

    # Single-value variables (not part of the cloud-cover ensemble) get
    # suffixed per model too when multiple models are requested - resolve
    # each to one representative series up front. See _hourly_series().
    precip_series = _hourly_series(hourly, "precipitation_probability")
    temp_series = _hourly_series(hourly, "temperature_2m")
    dewpoint_series = _hourly_series(hourly, "dewpoint_2m")
    wind_series = _hourly_series(hourly, "windspeed_10m")
    gust_series = _hourly_series(hourly, "windgusts_10m")
    humidity_series = _hourly_series(hourly, "relative_humidity_2m")

    precip_probs = []
    temps, dewpoints, winds, gusts, humidities = [], [], [], [], []
    for i, t in enumerate(times):
        dt = parse_iso_local(t, tz)
        if not (start <= dt <= end):
            continue
        for m in models:
            key = f"cloudcover_{m}"
            if key not in hourly and m == "best_match":
                key = "cloudcover"
            if key in hourly and hourly[key][i] is not None:
                per_model_cloud[m].append(hourly[key][i])
        if precip_series and precip_series[i] is not None:
            precip_probs.append(precip_series[i])
        if temp_series and temp_series[i] is not None:
            temps.append(temp_series[i])
        if dewpoint_series and dewpoint_series[i] is not None:
            dewpoints.append(dewpoint_series[i])
        if wind_series and wind_series[i] is not None:
            winds.append(wind_series[i])
        if gust_series and gust_series[i] is not None:
            gusts.append(gust_series[i])
        if humidity_series and humidity_series[i] is not None:
            humidities.append(humidity_series[i])

    model_means = {m: round(statistics.mean(v), 1) for m, v in per_model_cloud.items() if v}
    return {
        "model_cloud_means": model_means,
        "max_precip_probability": max(precip_probs) if precip_probs else None,
        "avg_temp": round(statistics.mean(temps), 1) if temps else None,
        "avg_dewpoint": round(statistics.mean(dewpoints), 1) if dewpoints else None,
        "avg_wind": round(statistics.mean(winds), 1) if winds else None,
        "max_gust": round(max(gusts), 1) if gusts else None,
        "avg_humidity": round(statistics.mean(humidities), 1) if humidities else None,
    }


def collect_yr_series(data, tz, start, end):
    cloud_vals = []
    wind_vals_ms = []
    rain_mm = 0.0
    for entry in data["properties"]["timeseries"]:
        dt = datetime.fromisoformat(entry["time"].replace("Z", "+00:00")).astimezone(tz)
        if not (start <= dt <= end):
            continue
        details = entry["data"].get("instant", {}).get("details", {})
        if "cloud_area_fraction" in details:
            cloud_vals.append(details["cloud_area_fraction"])
        if "wind_speed" in details:
            wind_vals_ms.append(details["wind_speed"])
        next1 = entry["data"].get("next_1_hours", {}).get("details", {})
        if "precipitation_amount" in next1:
            rain_mm += next1["precipitation_amount"]
    return {
        "cloud_mean": round(statistics.mean(cloud_vals), 1) if cloud_vals else None,
        "wind_mean_kmh": round(statistics.mean(wind_vals_ms) * 3.6, 1) if wind_vals_ms else None,
        "total_rain_mm": round(rain_mm, 2),
    }


def collect_7timer_series(data, tz, start, end):
    init_str = data["init"]
    init_dt = datetime.strptime(init_str, "%Y%m%d%H").replace(tzinfo=timezone.utc)
    seeing_vals, transparency_vals, cloud_vals = [], [], []
    rain_flag = False
    for pt in data["dataseries"]:
        dt = (init_dt + timedelta(hours=pt["timepoint"])).astimezone(tz)
        if not (start <= dt <= end):
            continue
        cloud_vals.append(pt["cloudcover"])
        seeing_vals.append(pt["seeing"])
        transparency_vals.append(pt["transparency"])
        if pt.get("prec_type") and pt["prec_type"] != "none":
            rain_flag = True
    def scale_word(vals, labels):
        if not vals:
            return None
        avg = statistics.mean(vals)
        idx = min(int((avg - 1) / 2), len(labels) - 1)
        return labels[max(idx, 0)]
    return {
        "cloud_okta_mean": round(statistics.mean(cloud_vals), 1) if cloud_vals else None,
        "seeing": scale_word(seeing_vals, ["Excellent", "Good", "Average", "Poor"]),
        "transparency": scale_word(transparency_vals, ["Excellent", "Good", "Average", "Poor"]),
        "rain_flag": rain_flag,
    }


def collect_meteoblue_series(data, tz, start, end):
    if not data or "data_1h" not in data:
        return None
    d = data["data_1h"]
    cloud_vals, precip_probs = [], []
    for i, t in enumerate(d.get("time", [])):
        try:
            dt = datetime.strptime(t, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        except ValueError:
            continue
        if not (start <= dt <= end):
            continue
        if "totalcloudcover" in d and d["totalcloudcover"][i] is not None:
            cloud_vals.append(d["totalcloudcover"][i])
        if "precipitation_probability" in d and d["precipitation_probability"][i] is not None:
            precip_probs.append(d["precipitation_probability"][i])
    return {
        "cloud_mean": round(statistics.mean(cloud_vals), 1) if cloud_vals else None,
        "max_precip_probability": max(precip_probs) if precip_probs else None,
    }


def collect_metservice_series(data, tz, start, end):
    times = data.get("dimensions", {}).get("time", {}).get("data", [])
    cloud_data = data.get("variables", {}).get("cloud.cover", {}).get("data", [])
    precip_data = data.get("variables", {}).get("precipitation.rate", {}).get("data", [])
    cloud_vals = []
    max_rate = 0.0
    for i, t in enumerate(times):
        dt = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(tz)
        if not (start <= dt <= end):
            continue
        if i < len(cloud_data) and cloud_data[i] is not None:
            cloud_vals.append(cloud_data[i])
        if i < len(precip_data) and precip_data[i] is not None:
            max_rate = max(max_rate, precip_data[i])
    return {
        "cloud_mean": round(statistics.mean(cloud_vals), 1) if cloud_vals else None,
        "max_precip_rate_mm_hr": round(max_rate, 2),
    }


# --------------------------------------------------------------------------
# Report building
# --------------------------------------------------------------------------
def _hour_bucket(dt):
    """Round a datetime down to the start of its local hour, so values from
    different sources (which may report a few minutes off from each other)
    line up on the same hourly slot."""
    return dt.replace(minute=0, second=0, microsecond=0)


def collect_hourly_timeline(om_data, yr_data, mb_data, ms_data, tz, start, end):
    """Build an hour-by-hour ensemble cloud-cover series across the dark
    window by combining every source that has hourly data, instead of
    collapsing the whole night into one average. This is what lets us find
    the best contiguous clear stretch (and flag when clouds are expected to
    roll in), rather than reporting a single blended verdict that can hide
    a night that starts clear and clouds over (or vice versa)."""
    hourly_points = {}

    hourly = om_data.get("hourly", {}) if om_data else {}
    times = hourly.get("time", [])
    models = om_data.get("_models_requested", ["best_match"]) if om_data else []
    for i, t in enumerate(times):
        dt = parse_iso_local(t, tz)
        if not (start <= dt <= end):
            continue
        vals = []
        for m in models:
            key = f"cloudcover_{m}"
            if key not in hourly and m == "best_match":
                key = "cloudcover"
            if key in hourly and hourly[key][i] is not None:
                vals.append(hourly[key][i])
        if vals:
            hourly_points.setdefault(_hour_bucket(dt), []).append(statistics.mean(vals))

    if yr_data:
        for entry in yr_data.get("properties", {}).get("timeseries", []):
            dt = datetime.fromisoformat(entry["time"].replace("Z", "+00:00")).astimezone(tz)
            if not (start <= dt <= end):
                continue
            details = entry.get("data", {}).get("instant", {}).get("details", {})
            if "cloud_area_fraction" in details:
                hourly_points.setdefault(_hour_bucket(dt), []).append(details["cloud_area_fraction"])

    if mb_data and "data_1h" in mb_data:
        d = mb_data["data_1h"]
        for i, t in enumerate(d.get("time", [])):
            try:
                dt = datetime.strptime(t, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
            except ValueError:
                continue
            if not (start <= dt <= end):
                continue
            if "totalcloudcover" in d and d["totalcloudcover"][i] is not None:
                hourly_points.setdefault(_hour_bucket(dt), []).append(d["totalcloudcover"][i])

    if ms_data:
        times_ms = ms_data.get("dimensions", {}).get("time", {}).get("data", [])
        cloud_data = ms_data.get("variables", {}).get("cloud.cover", {}).get("data", [])
        for i, t in enumerate(times_ms):
            dt = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(tz)
            if not (start <= dt <= end):
                continue
            if i < len(cloud_data) and cloud_data[i] is not None:
                hourly_points.setdefault(_hour_bucket(dt), []).append(cloud_data[i])

    return [
        {"time": hour.isoformat(), "cloud": round(statistics.mean(vals), 1), "sources": len(vals)}
        for hour, vals in sorted(hourly_points.items())
    ]


def find_best_window(timeline, clear_threshold):
    """Find the longest contiguous run of hours below clear_threshold% cloud
    cover. Each timeline point represents the hour starting at its
    timestamp, so a run from point i to point j covers [i.time, j.time+1h)."""
    if not timeline:
        return None
    points = [(datetime.fromisoformat(p["time"]), p["cloud"]) for p in timeline]
    best = None
    i = 0
    n = len(points)
    while i < n:
        if points[i][1] < clear_threshold:
            j = i
            while j < n and points[j][1] < clear_threshold:
                j += 1
            run_start = points[i][0]
            run_end = points[j - 1][0] + timedelta(hours=1)
            run_clouds = [points[k][1] for k in range(i, j)]
            run = {
                "start": run_start.isoformat(),
                "end": run_end.isoformat(),
                "hours": round((run_end - run_start).total_seconds() / 3600, 1),
                "avg_cloud": round(statistics.mean(run_clouds), 1),
            }
            if best is None or run["hours"] > best["hours"]:
                best = run
            i = j
        else:
            i += 1
    return best


def describe_window_trend(best_window, start, end):
    """A short, plain-language sentence about how conditions change across
    the night relative to the best clear stretch found."""
    if not best_window:
        return "No sustained clear stretch found overnight - expect cloud on and off all night."
    bw_start = datetime.fromisoformat(best_window["start"])
    bw_end = datetime.fromisoformat(best_window["end"])
    starts_at_dusk = bw_start <= start + timedelta(minutes=45)
    ends_at_dawn = bw_end >= end - timedelta(minutes=45)
    if starts_at_dusk and ends_at_dawn:
        return "Conditions look consistently good for the whole night."
    if starts_at_dusk:
        return f"Best right after dark, then increasing cloud after {bw_end.strftime('%-I:%M%p').lower()}."
    if ends_at_dawn:
        return f"Cloudier earlier on, clearing from around {bw_start.strftime('%-I:%M%p').lower()} onward."
    return (f"Best window is mid-night, {bw_start.strftime('%-I:%M%p').lower()}"
            f"\u2013{bw_end.strftime('%-I:%M%p').lower()}, with more cloud before and after.")


def build_report(config):
    lat, lon = config["latitude"], config["longitude"]
    tz = ZoneInfo(config["timezone"])
    contact_email = config.get("contact_email", config["recipient"])
    sources_status = {}

    om_data = None
    try:
        om_data = fetch_open_meteo(lat, lon, config["timezone"])
        sources_status["Open-Meteo"] = "ok"
    except Exception as e:
        log(f"Open-Meteo failed: {e}")
        sources_status["Open-Meteo"] = f"failed: {e}"

    if om_data is None:
        raise RuntimeError("Open-Meteo is required for sunrise/sunset and failed; aborting report")

    civil_sunset, civil_sunrise = night_window(om_data, tz)
    start, end = civil_sunset, civil_sunrise
    try:
        now_local = datetime.now(tz)
        start, end = astro_dark_window(lat, lon, tz, now_local)
        sources_status["Astronomical twilight (sunrise-sunset.org)"] = "ok"
    except Exception as e:
        log(f"Twilight lookup failed: {e}, falling back to civil sunset/sunrise")
        sources_status["Astronomical twilight (sunrise-sunset.org)"] = f"failed: {e}"
    dark_hours = round((end - start).total_seconds() / 3600, 1)
    om_summary = collect_open_meteo_series(om_data, tz, start, end)

    yr_summary = None
    yr_data = None
    try:
        yr_data = fetch_yr(lat, lon, contact_email)
        yr_summary = collect_yr_series(yr_data, tz, start, end)
        sources_status["YR.no (MET Norway)"] = "ok"
    except Exception as e:
        log(f"YR.no failed: {e}")
        sources_status["YR.no (MET Norway)"] = f"failed: {e}"

    seven_summary = None
    try:
        seven_data = fetch_7timer(lat, lon)
        seven_summary = collect_7timer_series(seven_data, tz, start, end)
        sources_status["7Timer! astro"] = "ok"
    except Exception as e:
        log(f"7Timer failed: {e}")
        sources_status["7Timer! astro"] = f"failed: {e}"

    mb_summary = None
    mb_data = None
    mb_key = get_secret("meteoblue_api_key")
    if mb_key:
        try:
            mb_data = fetch_meteoblue(lat, lon, mb_key)
            mb_summary = collect_meteoblue_series(mb_data, tz, start, end)
            sources_status["Meteoblue"] = "ok"
        except Exception as e:
            log(f"Meteoblue failed: {e}")
            sources_status["Meteoblue"] = f"failed: {e}"
    else:
        sources_status["Meteoblue"] = "no API key configured"

    ms_summary = None
    ms_data = None
    ms_key = get_secret("metservice_api_key")
    if ms_key:
        try:
            ms_data = fetch_metservice(lat, lon, ms_key)
            ms_summary = collect_metservice_series(ms_data, tz, start, end)
            sources_status["MetService (NZ)"] = "ok"
        except Exception as e:
            log(f"MetService failed: {e}")
            sources_status["MetService (NZ)"] = f"failed: {e}"
    else:
        sources_status["MetService (NZ)"] = "no API key configured"

    # Ensemble cloud cover across every source that produced a number
    cloud_points = list(om_summary["model_cloud_means"].values())
    if yr_summary and yr_summary["cloud_mean"] is not None:
        cloud_points.append(yr_summary["cloud_mean"])
    if mb_summary and mb_summary["cloud_mean"] is not None:
        cloud_points.append(mb_summary["cloud_mean"])
    if ms_summary and ms_summary["cloud_mean"] is not None:
        cloud_points.append(ms_summary["cloud_mean"])
    # 7Timer's okta scale (1-9) is roughly cloud% = (okta-1)/8*100
    if seven_summary and seven_summary["cloud_okta_mean"] is not None:
        cloud_points.append(max(0, min(100, (seven_summary["cloud_okta_mean"] - 1) / 8 * 100)))

    ensemble_mean = round(statistics.mean(cloud_points), 1) if cloud_points else None
    ensemble_spread = (round(max(cloud_points) - min(cloud_points), 1)
                        if len(cloud_points) > 1 else 0)

    rain_risk = False
    rain_notes = []
    if om_summary["max_precip_probability"] is not None and om_summary["max_precip_probability"] >= 30:
        rain_risk = True
        rain_notes.append(f"Open-Meteo max precip probability {om_summary['max_precip_probability']}%")
    if yr_summary and yr_summary["total_rain_mm"] and yr_summary["total_rain_mm"] > 0.2:
        rain_risk = True
        rain_notes.append(f"YR.no forecasts {yr_summary['total_rain_mm']}mm overnight")
    if seven_summary and seven_summary["rain_flag"]:
        rain_risk = True
        rain_notes.append("7Timer! flags precipitation overnight")
    if mb_summary and mb_summary["max_precip_probability"] is not None and mb_summary["max_precip_probability"] >= 30:
        rain_risk = True
        rain_notes.append(f"Meteoblue max precip probability {mb_summary['max_precip_probability']}%")
    if ms_summary and ms_summary["max_precip_rate_mm_hr"] and ms_summary["max_precip_rate_mm_hr"] > 0.2:
        rain_risk = True
        rain_notes.append(f"MetService forecasts up to {ms_summary['max_precip_rate_mm_hr']}mm/hr overnight")

    # Wind - often as much a threat to a night's imaging as cloud, since it
    # shakes the rig and disrupts autoguiding. Combine every source that
    # reports it into a simple mean, and separately track peak gusts.
    wind_avgs = []
    if om_summary.get("avg_wind") is not None:
        wind_avgs.append(om_summary["avg_wind"])
    if yr_summary and yr_summary.get("wind_mean_kmh") is not None:
        wind_avgs.append(yr_summary["wind_mean_kmh"])
    ensemble_wind_mean = round(statistics.mean(wind_avgs), 1) if wind_avgs else None
    max_gust = om_summary.get("max_gust")

    wind_warn_kmh = config.get("wind_warn_kmh", 20)
    wind_bad_kmh = config.get("wind_bad_kmh", 35)

    wind_risk = False
    wind_notes = []
    if ensemble_wind_mean is not None and ensemble_wind_mean >= wind_bad_kmh:
        wind_risk = True
        wind_notes.append(f"Average wind {ensemble_wind_mean} km/h - likely too windy for steady tracking/guiding")
    elif ensemble_wind_mean is not None and ensemble_wind_mean >= wind_warn_kmh:
        wind_notes.append(f"Average wind {ensemble_wind_mean} km/h - may affect guiding on exposed setups")
    if max_gust is not None and max_gust >= wind_bad_kmh * 1.3:
        wind_risk = True
        wind_notes.append(f"Gusts up to {max_gust} km/h")

    # Best clear-sky window - a whole-night average can hide a night that
    # starts clear and clouds over (or the reverse), so also look at the
    # hour-by-hour ensemble to find the longest contiguous clear stretch.
    clear_threshold_pct = config.get("clear_threshold_pct", 30)
    hourly_timeline = collect_hourly_timeline(om_data, yr_data, mb_data, ms_data, tz, start, end)
    best_window = find_best_window(hourly_timeline, clear_threshold_pct)
    best_window_note = describe_window_trend(best_window, start, end)

    if ensemble_mean is None:
        verdict = "Unable to determine — not enough data from any source."
    elif ensemble_mean < 20:
        verdict = "Great imaging conditions — mostly clear skies expected."
    elif ensemble_mean < 40:
        verdict = "Good — some intermittent cloud possible."
    elif ensemble_mean < 70:
        verdict = "Marginal — patchy cloud likely, keep an eye on it."
    else:
        verdict = "Poor — mostly cloudy, imaging unlikely to be worthwhile."

    if wind_risk:
        verdict += " Wind is also a concern tonight — expect a shaky rig."

    moon_illum, moon_name = moon_phase_info(datetime.now(timezone.utc))

    return {
        "generated_at": datetime.now(tz).isoformat(),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "civil_sunset": civil_sunset.isoformat(),
        "civil_sunrise": civil_sunrise.isoformat(),
        "dark_hours": dark_hours,
        "sources_status": sources_status,
        "open_meteo": om_summary,
        "yr": yr_summary,
        "seven_timer": seven_summary,
        "meteoblue": mb_summary,
        "metservice": ms_summary,
        "ensemble_cloud_mean": ensemble_mean,
        "ensemble_cloud_spread": ensemble_spread,
        "rain_risk": rain_risk,
        "rain_notes": rain_notes,
        "ensemble_wind_mean": ensemble_wind_mean,
        "max_gust": max_gust,
        "wind_risk": wind_risk,
        "wind_notes": wind_notes,
        "wind_warn_kmh": wind_warn_kmh,
        "wind_bad_kmh": wind_bad_kmh,
        "hourly_timeline": hourly_timeline,
        "best_window": best_window,
        "best_window_note": best_window_note,
        "clear_threshold_pct": clear_threshold_pct,
        "verdict": verdict,
        "moon_illumination": round(moon_illum, 1),
        "moon_phase": moon_name,
    }


# --------------------------------------------------------------------------
# Email rendering + sending
# --------------------------------------------------------------------------
def render_text(report, config):
    lines = []
    lines.append(f"Astrophotography Forecast — {config.get('location_name', 'your site')}")
    lines.append(f"Astronomical darkness: {report['window']['start']} to {report['window']['end']} ({report.get('dark_hours', '?')}h)")
    lines.append(f"(civil sunset {report.get('civil_sunset','?')} / sunrise {report.get('civil_sunrise','?')})")
    lines.append("")
    lines.append(f"VERDICT: {report['verdict']}")
    lines.append(f"Ensemble cloud cover: {report['ensemble_cloud_mean']}% (spread across sources: {report['ensemble_cloud_spread']} pts)")
    bw = report.get("best_window")
    clear_pct = report.get("clear_threshold_pct", 30)
    if bw:
        try:
            bw_start_disp = datetime.fromisoformat(bw["start"]).strftime("%-I:%M%p").lower()
            bw_end_disp = datetime.fromisoformat(bw["end"]).strftime("%-I:%M%p").lower()
        except Exception:
            bw_start_disp, bw_end_disp = bw["start"], bw["end"]
        lines.append(f"Best window: {bw_start_disp}-{bw_end_disp} ({bw['hours']}h below {clear_pct}% cloud, avg {bw['avg_cloud']}%)")
    else:
        lines.append(f"Best window: no sustained clear stretch found below {clear_pct}% cloud")
    if report.get("best_window_note"):
        lines.append(report["best_window_note"])
    if report["rain_risk"]:
        lines.append("RAIN RISK: " + "; ".join(report["rain_notes"]))
    else:
        lines.append("Rain risk: low")
    if report.get("wind_risk"):
        lines.append("WIND RISK: " + "; ".join(report.get("wind_notes", [])))
    elif report.get("wind_notes"):
        lines.append("Wind: " + "; ".join(report["wind_notes"]))
    else:
        gust_bit = f", gusts to {report['max_gust']} km/h" if report.get("max_gust") is not None else ""
        lines.append(f"Wind: {report.get('ensemble_wind_mean', '?')} km/h avg{gust_bit}")
    lines.append(f"Moon: {report['moon_phase']} ({report['moon_illumination']}% illuminated)")
    if report["seven_timer"]:
        st = report["seven_timer"]
        lines.append(f"Seeing: {st['seeing']}, Transparency: {st['transparency']} (7Timer!)")
    om = report["open_meteo"]
    if om.get("avg_temp") is not None:
        lines.append(f"Avg temp: {om['avg_temp']}°C, dew point: {om['avg_dewpoint']}°C, "
                      f"humidity: {om['avg_humidity']}%")
    lines.append("")
    lines.append("Per-model cloud cover (Open-Meteo):")
    for m, v in om["model_cloud_means"].items():
        lines.append(f"  {m}: {v}%")
    if report["yr"]:
        lines.append(f"YR.no (MET Norway) cloud cover: {report['yr']['cloud_mean']}%")
    if report["meteoblue"] and report["meteoblue"].get("cloud_mean") is not None:
        lines.append(f"Meteoblue cloud cover: {report['meteoblue']['cloud_mean']}%")
    elif report["meteoblue"] is not None:
        lines.append("Meteoblue: fetched but no overlapping hourly data found (see log)")
    if report.get("metservice") and report["metservice"].get("cloud_mean") is not None:
        lines.append(f"MetService (NZ) cloud cover: {report['metservice']['cloud_mean']}%")
    lines.append("")
    lines.append("Source status:")
    for name, status in report["sources_status"].items():
        lines.append(f"  {name}: {status}")
    return "\n".join(lines)


MODEL_DISPLAY_NAMES = {
    "best_match": "Best Match",
    "ecmwf_ifs025": "ECMWF",
    "gfs_seamless": "GFS (NOAA)",
    "icon_seamless": "ICON (DWD)",
    "ukmo_seamless": "UK Met Office",
    "meteofrance_seamless": "Météo-France",
    "jma_seamless": "JMA (Japan)",
    "gem_seamless": "GEM (Canada)",
    "bom_access_global": "BOM (Australia)",
}

MOON_EMOJI = {
    "New Moon": "\U0001F311",
    "Waxing Crescent": "\U0001F312",
    "First Quarter": "\U0001F313",
    "Waxing Gibbous": "\U0001F314",
    "Full Moon": "\U0001F315",
    "Waning Gibbous": "\U0001F316",
    "Last Quarter": "\U0001F317",
    "Waning Crescent": "\U0001F318",
}


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _cloud_color(pct):
    if pct is None:
        return "#5c6690"
    if pct < 20:
        return "#2e7d32"
    if pct < 40:
        return "#9e9d24"
    if pct < 70:
        return "#e07b00"
    return "#c62828"


def _wind_color(kmh, warn, bad):
    if kmh is None:
        return "#5c6690"
    if kmh < warn:
        return "#2e7d32"
    if kmh < bad:
        return "#e07b00"
    return "#c62828"


def _verdict_style(ensemble_mean):
    color = _cloud_color(ensemble_mean if ensemble_mean is not None else 100)
    if ensemble_mean is None:
        emoji = "❓"
    elif ensemble_mean < 20:
        emoji = "☀️"
    elif ensemble_mean < 40:
        emoji = "\U0001F324️"
    elif ensemble_mean < 70:
        emoji = "⛅"
    else:
        emoji = "☁️"
    return color, emoji


def _bar_row(label, pct):
    pct_display = "n/a" if pct is None else "{:.0f}%".format(pct)
    width = 1 if pct is None else max(2, min(100, pct))
    color = _cloud_color(pct)
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:8px;">'
        '<tr>'
        '<td width="120" style="color:#c7cfe8;font-size:12px;font-family:-apple-system,Helvetica,Arial,sans-serif;padding-right:8px;">' + _esc(label) + '</td>'
        '<td style="background:#1a2140;border-radius:5px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" width="' + str(width) + '%" style="border-collapse:collapse;">'
        '<tr><td style="background:' + color + ';height:12px;border-radius:5px;font-size:1px;line-height:1px;">&nbsp;</td></tr>'
        '</table>'
        '</td>'
        '<td width="44" style="color:#c7cfe8;font-size:12px;text-align:right;padding-left:8px;font-family:-apple-system,Helvetica,Arial,sans-serif;">' + pct_display + '</td>'
        '</tr>'
        '</table>'
    )


def _stat_card(label, value, sub="", value_color="#ffffff", width="50%"):
    sub_html = ('<div style="color:#6c7aa8;font-size:11px;margin-top:2px;">' + _esc(sub) + '</div>') if sub else ""
    return (
        '<td width="' + width + '" style="padding:6px;" valign="top">'
        '<div style="background:#1a2140;border-radius:8px;padding:12px 14px;font-family:-apple-system,Helvetica,Arial,sans-serif;">'
        '<div style="color:#9aa4c7;font-size:11px;letter-spacing:.04em;">' + _esc(label) + '</div>'
        '<div style="color:' + value_color + ';font-size:20px;font-weight:700;margin-top:2px;">' + str(value) + '</div>'
        + sub_html +
        '</div>'
        '</td>'
    )


def render_html(report, config):
    ensemble_mean = report.get("ensemble_cloud_mean")
    verdict_color, verdict_emoji = _verdict_style(ensemble_mean)
    moon_phase = report.get("moon_phase", "")
    moon_emoji = MOON_EMOJI.get(moon_phase, "\U0001F311")
    location_name = _esc(config.get("location_name", "your site"))

    try:
        sunset_disp = datetime.fromisoformat(report["window"]["start"]).strftime("%-I:%M%p").lower()
        sunrise_disp = datetime.fromisoformat(report["window"]["end"]).strftime("%-I:%M%p").lower()
        civil_sunset_disp = datetime.fromisoformat(report["civil_sunset"]).strftime("%-I:%M%p").lower() if report.get("civil_sunset") else None
        civil_sunrise_disp = datetime.fromisoformat(report["civil_sunrise"]).strftime("%-I:%M%p").lower() if report.get("civil_sunrise") else None
    except Exception:
        sunset_disp = report["window"]["start"]
        sunrise_disp = report["window"]["end"]
        civil_sunset_disp = civil_sunrise_disp = None

    if report.get("rain_risk"):
        rain_color, rain_text = "#e07b00", "Possible — see notes below"
    else:
        rain_color, rain_text = "#2e7d32", "Low"

    wind_warn_kmh = report.get("wind_warn_kmh", 20)
    wind_bad_kmh = report.get("wind_bad_kmh", 35)
    ensemble_wind_mean = report.get("ensemble_wind_mean")
    wind_color = _wind_color(ensemble_wind_mean, wind_warn_kmh, wind_bad_kmh)
    wind_value = "{} km/h".format(ensemble_wind_mean) if ensemble_wind_mean is not None else "n/a"
    wind_sub_bits = []
    if report.get("max_gust") is not None:
        wind_sub_bits.append("gusts to {} km/h".format(report["max_gust"]))
    if report.get("wind_notes"):
        wind_sub_bits.append("; ".join(report["wind_notes"]))
    wind_sub = " · ".join(wind_sub_bits) if wind_sub_bits else "avg over the dark window"

    seven = report.get("seven_timer") or {}
    seeing = seven.get("seeing", "n/a")
    transparency = seven.get("transparency", "n/a")

    spread_sub = ("models disagree by {} pts".format(report.get('ensemble_cloud_spread', 0))
                  if report.get("ensemble_cloud_spread") else "")
    cloud_card = _stat_card("CLOUD COVER (ensemble)",
                             "{}%".format(ensemble_mean) if ensemble_mean is not None else "n/a",
                             spread_sub)
    wind_card = _stat_card("WIND", wind_value, wind_sub, value_color=wind_color)
    moon_card = _stat_card("MOON", "{} {}%".format(moon_emoji, report.get('moon_illumination', '?')), moon_phase)
    seeing_card = _stat_card("SEEING / TRANSPARENCY", "{} / {}".format(seeing, transparency), "7Timer! astro forecast")
    rain_sub = "; ".join(report.get("rain_notes", [])) or "no precipitation flagged"
    rain_card = _stat_card("RAIN RISK", rain_text, rain_sub, value_color=rain_color)

    clear_threshold_pct = report.get("clear_threshold_pct", 30)
    bw = report.get("best_window")
    if bw:
        try:
            bw_start_disp = datetime.fromisoformat(bw["start"]).strftime("%-I:%M%p").lower()
            bw_end_disp = datetime.fromisoformat(bw["end"]).strftime("%-I:%M%p").lower()
        except Exception:
            bw_start_disp, bw_end_disp = bw["start"], bw["end"]
        best_window_value = "{}\u2013{}".format(bw_start_disp, bw_end_disp)
        best_window_sub = "{}h below {}% cloud · {}".format(
            bw["hours"], clear_threshold_pct, report.get("best_window_note", ""))
        best_window_color = _cloud_color(bw.get("avg_cloud"))
    else:
        best_window_value = "n/a"
        best_window_sub = report.get("best_window_note") or "no sustained clear stretch found tonight"
        best_window_color = "#5c6690"
    best_window_card = _stat_card("BEST WINDOW TONIGHT", best_window_value, best_window_sub,
                                   value_color=best_window_color, width="100%")

    hour_bars = ""
    for point in report.get("hourly_timeline", []):
        try:
            hour_label = datetime.fromisoformat(point["time"]).strftime("%-I%p").lower()
        except Exception:
            hour_label = point["time"]
        hour_bars += _bar_row(hour_label, point["cloud"])

    bars = "".join(
        _bar_row(MODEL_DISPLAY_NAMES.get(m, m), pct)
        for m, pct in report["open_meteo"]["model_cloud_means"].items()
    )
    if report.get("yr") and report["yr"].get("cloud_mean") is not None:
        bars += _bar_row("YR.no (Norway)", report["yr"]["cloud_mean"])
    if report.get("meteoblue") and report["meteoblue"].get("cloud_mean") is not None:
        bars += _bar_row("Meteoblue", report["meteoblue"]["cloud_mean"])
    if report.get("metservice") and report["metservice"].get("cloud_mean") is not None:
        bars += _bar_row("MetService (NZ)", report["metservice"]["cloud_mean"])

    om = report["open_meteo"]
    conditions_bits = []
    if om.get("avg_temp") is not None:
        conditions_bits.append("Temp {}°C".format(om['avg_temp']))
    if om.get("avg_dewpoint") is not None:
        conditions_bits.append("Dew point {}°C".format(om['avg_dewpoint']))
    if om.get("avg_humidity") is not None:
        conditions_bits.append("Humidity {}%".format(om['avg_humidity']))
    conditions_line = " &middot; ".join(conditions_bits)

    source_status_line = " &middot; ".join(
        "{}: {}".format(_esc(name), ('✓' if status == 'ok' else '✗'))
        for name, status in report.get("sources_status", {}).items()
    )

    verdict_text = _esc(report.get("verdict", ""))

    parts = []
    parts.append('<div style="background:#0b1020;padding:24px 12px;">')
    parts.append('<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">')
    parts.append('<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#12172b;border-radius:12px;overflow:hidden;">')
    parts.append('<tr><td style="background:#1a2140;padding:22px 24px;font-family:-apple-system,Helvetica,Arial,sans-serif;">')
    parts.append('<div style="color:#8fa3ff;font-size:12px;letter-spacing:.08em;text-transform:uppercase;">Astrophotography Forecast</div>')
    parts.append('<div style="color:#ffffff;font-size:21px;font-weight:700;margin-top:4px;">' + location_name + '</div>')
    civil_line = (' (civil sunset ' + civil_sunset_disp + ' / sunrise ' + civil_sunrise_disp + ')') if civil_sunset_disp else ''
    dark_hours_disp = report.get("dark_hours")
    hours_line = (' &mdash; ' + str(dark_hours_disp) + 'h dark') if dark_hours_disp is not None else ''
    parts.append('<div style="color:#9aa4c7;font-size:13px;margin-top:3px;">Astronomical darkness: ' + sunset_disp + ' &ndash; ' + sunrise_disp + hours_line + '</div>')
    parts.append('<div style="color:#6c7aa8;font-size:11px;margin-top:2px;">civil sunset ' + (civil_sunset_disp or "?") + ' / sunrise ' + (civil_sunrise_disp or "?") + '</div>')
    parts.append('</td></tr>')
    parts.append('<tr><td style="background:' + verdict_color + ';padding:16px 24px;font-family:-apple-system,Helvetica,Arial,sans-serif;">')
    parts.append('<span style="font-size:22px;vertical-align:middle;">' + verdict_emoji + '</span>')
    parts.append('<span style="font-size:16px;font-weight:700;color:#ffffff;vertical-align:middle;margin-left:8px;">' + verdict_text + '</span>')
    parts.append('</td></tr>')
    parts.append('<tr><td style="padding:14px 18px 0 18px;">')
    parts.append('<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>' + best_window_card + '</tr></table>')
    parts.append('</td></tr>')
    parts.append('<tr><td style="padding:16px 18px 4px 18px;">')
    parts.append('<table role="presentation" width="100%" cellpadding="0" cellspacing="0">')
    parts.append('<tr>' + cloud_card + wind_card + '</tr>')
    dark_card = _stat_card("HOURS OF DARKNESS", (str(report.get("dark_hours", "?")) + "h"), "astronomical twilight to twilight")
    parts.append('<tr>' + rain_card + dark_card + '</tr>')
    parts.append('<tr>' + moon_card + seeing_card + '</tr>')
    parts.append('</table>')
    parts.append('</td></tr>')
    parts.append('<tr><td style="padding:12px 24px 4px 24px;">')
    parts.append('<div style="color:#9aa4c7;font-size:11px;letter-spacing:.04em;font-family:-apple-system,Helvetica,Arial,sans-serif;margin-bottom:10px;">CLOUD COVER BY HOUR TONIGHT</div>')
    parts.append(hour_bars if hour_bars else '<div style="color:#5c6690;font-size:12px;font-family:-apple-system,Helvetica,Arial,sans-serif;">Not enough hourly data to break this down.</div>')
    parts.append('</td></tr>')
    parts.append('<tr><td style="padding:4px 24px 4px 24px;">')
    parts.append('<div style="color:#9aa4c7;font-size:11px;letter-spacing:.04em;font-family:-apple-system,Helvetica,Arial,sans-serif;margin-bottom:10px;">CLOUD COVER BY SOURCE</div>')
    parts.append(bars)
    parts.append('</td></tr>')
    parts.append('<tr><td style="padding:4px 24px 20px 24px;">')
    parts.append('<div style="color:#9aa4c7;font-size:12px;font-family:-apple-system,Helvetica,Arial,sans-serif;">' + conditions_line + '</div>')
    parts.append('</td></tr>')
    parts.append('<tr><td style="background:#0e1226;padding:14px 24px;">')
    parts.append('<div style="color:#5c6690;font-size:11px;font-family:-apple-system,Helvetica,Arial,sans-serif;">' + source_status_line + '</div>')
    parts.append('<div style="color:#3d4670;font-size:10px;margin-top:4px;font-family:-apple-system,Helvetica,Arial,sans-serif;">Generated ' + _esc(report.get("generated_at","")) + '</div>')
    parts.append('</td></tr>')
    parts.append('</table>')
    parts.append('</td></tr></table>')
    parts.append('</div>')
    return "".join(parts)


def render_html_legacy(report, config):
    text = render_text(report, config)
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return "<pre style='font-family: -apple-system, sans-serif; white-space: pre-wrap;'>" + escaped + "</pre>"



def send_email(subject, text_body, html_body, config):
    sender = config["sender"]
    recipient = config["recipient"]
    password = get_secret("gmail_app_password")
    if not password:
        raise RuntimeError(
            "No Gmail app password found. Add it to secrets.json as 'gmail_app_password' "
            "or store it in Keychain under service 'apd-weather', account 'gmail_app_password'."
        )
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(sender, password)
        server.sendmail(sender, [recipient], msg.as_string())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Build the report and print it, but don't send email")
    args = parser.parse_args()

    config = load_config()
    try:
        report = build_report(config)
    except Exception:
        log("FATAL: failed to build report\n" + traceback.format_exc())
        sys.exit(1)

    text_body = render_text(report, config)
    html_body = render_html(report, config)
    subject = f"\U0001F30C Astro Forecast: {report['verdict'].split(chr(0x2014))[0].strip()} (Moon {report['moon_illumination']}%)"

    if args.dry_run:
        print(text_body)
        return

    try:
        send_email(subject, text_body, html_body, config)
        log("Email sent successfully")
    except Exception:
        log("FATAL: failed to send email\n" + traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
