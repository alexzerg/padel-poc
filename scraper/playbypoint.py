#!/usr/bin/env python3
"""Playbypoint availability collector.

Why a second collector: the clubs that run on Playbypoint (Reserve, Crown,
Wynwood, Champions, ...) are invisible to the Playtomic scraper. Playbypoint
exposes a real JSON API, so this collector needs no browser at all - only a
TLS fingerprint that Cloudflare accepts (curl_cffi impersonate=chrome) and a
logged-in `remember_user_token` cookie, because every /api/ route is 403 for
anonymous callers.

Indoor/outdoor comes from the court's own `surface` field (padel_court_indoor /
padel_court_outdoor) or its name - never guessed when the API states it.
"""
import json
import os
import pathlib
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from curl_cffi import requests

BASE = os.getenv("PBP_BASE", "https://app.playbypoint.com")
TOKEN = os.getenv("PBP_REMEMBER_TOKEN", "").strip()
IMPERSONATE = os.getenv("PBP_IMPERSONATE", "chrome")
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "7"))
API_INGEST = os.getenv("API_INGEST", "http://padel/api/ingest")
CLUBS = pathlib.Path(os.getenv("CLUBS_FILE", "/srv/clubs.json"))
TZ = ZoneInfo(os.getenv("CLUB_TZ", "America/New_York"))
SLEEP = float(os.getenv("PBP_SLEEP", "0.25"))
H = {"Accept": "application/json"}


def session() -> requests.Session:
    s = requests.Session(impersonate=IMPERSONATE)
    if TOKEN:
        s.cookies.set("remember_user_token", TOKEN, domain=".playbypoint.com")
    return s


def get(s: requests.Session, path: str, tries: int = 3):
    """GET with retry. 403 means the session cookie died - fail loudly, do not
    silently report the club as empty."""
    last = ""
    for n in range(tries):
        try:
            r = s.get(BASE + path, headers=H, timeout=25)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
            if r.status_code == 403:
                raise PermissionError("403 - remember_user_token rejected or expired")
        except PermissionError:
            raise
        except Exception as exc:  # noqa: BLE001
            last = str(exc)[:120]
        time.sleep(1.5 * (n + 1))
    raise RuntimeError(f"{path}: {last}")


def hhmm(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def label_minutes(part: str, meridiem: str) -> int | None:
    part = part.strip().lower().replace("am", "").replace("pm", "").strip()
    if not part:
        return None
    h, _, mm = part.partition(":")
    try:
        hour = int(h)
        minute = int(mm) if mm else 0
    except ValueError:
        return None
    if meridiem == "pm" and hour != 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    return hour * 60 + minute


def duration_of(schedule: str, start_min: int) -> int | None:
    """'7-8:30am' + start 07:00 -> 90. Start comes from the API's
    seconds_from_midnight, so only the end has to be parsed."""
    s = (schedule or "").strip().lower()
    if "-" not in s:
        return None
    end_raw = s.split("-", 1)[1]
    meridiem = "pm" if "pm" in end_raw else ("am" if "am" in end_raw else "")
    end = label_minutes(end_raw, meridiem)
    if end is None:
        return None
    while end <= start_min:
        end += 720
    return end - start_min


def court_facts(court: dict, club_default: bool) -> dict:
    surface = (court.get("surface") or "").lower()
    name = (court.get("name") or "").lower()
    if "indoor" in surface or "outdoor" in surface:
        return {"indoor": "indoor" in surface, "indoor_source": "court-surface"}
    if "indoor" in name or "outdoor" in name:
        return {"indoor": "indoor" in name, "indoor_source": "court-name"}
    return {"indoor": bool(club_default), "indoor_source": "club-default"}


def hours_of(payload) -> list:
    if isinstance(payload, dict):
        return payload.get("available_hours") or []
    return payload or []


def collect() -> dict:
    clubs = [c for c in json.loads(CLUBS.read_text())
             if c.get("feed") == "playbypoint" and c.get("facility_id")]
    print(f"playbypoint: {len(clubs)} clubs, {DAYS_AHEAD} days ahead, token="
          f"{'set' if TOKEN else 'MISSING'}", flush=True)
    s = session()
    today = datetime.now(TZ).date()
    days = [(today + timedelta(days=i)).isoformat() for i in range(DAYS_AHEAD)]
    rows: list[dict] = []
    report: list[dict] = []
    errors: list[str] = []
    for club in clubs:
        fid = club["facility_id"]
        try:
            courts = [c for c in get(s, f"/api/facilities/{fid}/courts")
                      if not c.get("archived")
                      and ("padel" in (c.get("surface") or "").lower()
                           or "padel" in (c.get("name") or "").lower())]
        except Exception as exc:  # noqa: BLE001
            msg = f"{club['name']}: courts: {exc}"
            print(f"   ERROR {msg}", flush=True)
            errors.append(msg)
            for day in days:
                report.append({"club": club["name"], "date": day, "status": "error",
                               "slots": 0, "detail": str(exc)[:160]})
            continue
        print(f" - {club['name']} (facility {fid}): {len(courts)} padel courts", flush=True)
        for day in days:
            noon = datetime.fromisoformat(day + "T12:00:00").replace(tzinfo=TZ)
            ts = int(noon.timestamp())
            day_rows: list[dict] = []
            seen_courts = 0
            failed = None
            for court in courts:
                try:
                    hours = hours_of(get(s, f"/api/courts/{court['id']}/available_hours"
                                            f"?timestamp={ts}"))
                except Exception as exc:  # noqa: BLE001
                    failed = str(exc)[:160]
                    break
                seen_courts += 1
                facts = court_facts(court, club.get("indoor"))
                for h in hours:
                    if not h.get("available"):
                        continue
                    start_min = int(float(h.get("seconds_from_midnight") or 0) // 60)
                    day_rows.append({
                        "date": day,
                        "club": club["name"],
                        "area": club.get("area", ""),
                        "drive": f"{club.get('miles')} mi from 33020",
                        "indoor": facts["indoor"],
                        "indoor_source": facts["indoor_source"],
                        "court_type": "",
                        "wall": "",
                        "start": hhmm(start_min * 60),
                        "duration": duration_of(h.get("schedule", ""), start_min),
                        "price": None,
                        "court": court.get("name") or "",
                        "url": club.get("book_url", ""),
                    })
                time.sleep(SLEEP)
            if failed:
                errors.append(f"{club['name']} {day}: {failed}")
                report.append({"club": club["name"], "date": day, "status": "error",
                               "slots": 0, "detail": failed})
                continue
            if day_rows:
                status = "ok"
            elif club.get("access") == "members-only":
                status = "members-only"
            else:
                status = "no-slots"
            rows.extend(day_rows)
            report.append({
                "club": club["name"], "date": day, "status": status,
                "slots": len(day_rows), "courts_seen": seen_courts,
                "resources_seen": len({r["court"] for r in day_rows}),
                "indoor_slots": sum(1 for r in day_rows if r["indoor"]),
                "outdoor_slots": sum(1 for r in day_rows if not r["indoor"]),
                "source": "playbypoint",
                "court_detail": [f"{c['name']} ({c['surface']})" for c in courts][:12],
            })
            print(f"   {day} {club['name']}: {len(day_rows)} free slots "
                  f"({status})", flush=True)
    return {
        "source": "playbypoint",
        "scanned_at": datetime.now(tz=ZoneInfo("UTC")).isoformat(timespec="seconds"),
        "slots": rows,
        "report": report,
        "errors": errors,
        "partial": True,
        "clubs": [c["name"] for c in clubs],
    }


def main() -> int:
    payload = collect()
    print(f"total: {len(payload['slots'])} slots, {len(payload['report'])} club/day scans, "
          f"{len(payload['errors'])} errors", flush=True)
    if not payload["report"]:
        print("nothing collected - not ingesting", flush=True)
        return 1
    req = urllib.request.Request(API_INGEST, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
        print("ingest:", resp.status, resp.read().decode()[:300], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
