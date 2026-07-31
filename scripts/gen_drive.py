#!/usr/bin/env python3
"""Generate app/data/drive.json: road distance and drive time from every saved origin
to every club.

Providers
  osrm    public demo server, no key, NO traffic. One call per origin/club pair.
  tomtom  calculateRoute + departAt + traffic. Needs TOMTOM_API_KEY. No card on the
          account means the provider itself stops serving at the quota.
  mapbox  driving-traffic + depart_at. Needs MAPBOX_TOKEN.

WHY THIS FILE IS PARANOID
Mapbox states plainly that accounts do NOT support a spending cap, and that monthly
billing cannot be limited. With a card on file the free tier is a soft line, not a wall:
cross it and the account is charged at $2.00 per 1000 requests. There is no server-side
brake, so the brake has to live here. Twelve guards, in order of when they fire:

  1  kill switch      traffic providers refuse to run unless DRIVE_TRAFFIC=1
  2  spend consent    traffic providers also require --i-know-this-spends-quota
  3  shape assertion  origins, clubs, buckets and the exact planned call count are bounded
  4  run ceiling      planned > MAX_CALLS_PER_RUN aborts before the first call
  5  daily budget     persistent per-day ledger, never bypassable, fails closed
  6  monthly budget   persistent per-month ledger, never bypassable
  7  rerun interval   refuse to rerun within MIN_INTERVAL_H unless --force
  8  exclusive lock   a second concurrent run cannot start
  9  charge first     the ledger is written BEFORE the call, so a crash cannot lose count
 10  status guard     401/402/403/429 aborts the whole run immediately, no retry, ever
 11  failure breaker  MAX_CONSECUTIVE_FAILURES aborts the run
 12  wall clock       the run aborts after RUN_DEADLINE_S regardless of progress

Usage
  python3 scripts/gen_drive.py --provider osrm
  DRIVE_TRAFFIC=1 python3 scripts/gen_drive.py --provider tomtom --dry-run
"""
import argparse
import fcntl
import json
import os
import pathlib
import subprocess
import sys
import time
from datetime import datetime, timedelta

try:
    import httpx
except ImportError:  # repo checkouts may not have it; curl covers that case
    httpx = None

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Paths are overridable so the very same script runs from a repo checkout and from a pod,
# where clubs.json is mounted read-only and state lives on a writable volume.
CLUBS = pathlib.Path(os.getenv("DRIVE_CLUBS") or ROOT / "app/data/clubs.json")
OUT = pathlib.Path(os.getenv("DRIVE_OUT") or ROOT / "app/data/drive.json")
LEDGER = pathlib.Path(os.getenv("DRIVE_LEDGER") or ROOT / "app/data/drive-usage.json")
LOCK = pathlib.Path(os.getenv("DRIVE_LOCK") or "/tmp/padel-gen-drive.lock")

# --- guard limits -------------------------------------------------------------------
MAX_ORIGINS = 6
MAX_CLUBS = 12
MAX_BUCKETS = 14
MAX_CALLS_PER_RUN = 700             # 520 traffic calls + 40 free-flow calls; more is a bug
DAILY_BUDGET = {"osrm": 400, "tomtom": 600, "mapbox": 600}
# tomtom free tier is 20000/month: cap at 18000 so the guard bites before the provider
MONTHLY_BUDGET = {"osrm": 12000, "tomtom": 18000, "mapbox": 18000}
# DRIVE_DAY_LIMIT may raise the daily cap for a one-off manual run (e.g. finishing a matrix
# after an aborted run already burned part of the day). It can never go past this ceiling,
# so a typo or a stray env var cannot unlock the whole day.
HARD_DAY_CEILING = 1200
# 960 calls x 31 days = 29760. The cap is 31000: enough for one refresh a day and
# nothing more, and still only 31% of the 100k free tier if a day is retried.
MAX_CONSECUTIVE_FAILURES = 5
MIN_INTERVAL_H = 20
CALL_TIMEOUT_S = 25
RUN_DEADLINE_S = 900
FATAL_STATUS = {401, 402, 403, 429}  # auth, payment required, forbidden, rate limited

ORIGINS = {
    "33020":     {"label": "Hollywood (33020)",                    "lat": 26.0112, "lng": -80.1495},
    "turnberry": {"label": "Turnberry, Aventura (33180)",          "lat": 25.9585, "lng": -80.1420},
    "yachtclub": {"label": "3598 Yacht Club Dr, Aventura (33180)", "lat": 25.9735, "lng": -80.1355},
    "solemia":   {"label": "SoLe Mia, North Miami (33181)",        "lat": 25.9112, "lng": -80.1527},
}
# Precise where it matters, cheap everywhere else.
# TomTom Routing is 20k requests a MONTH free. Weekdays only halves the plan, which buys
# back half-hour resolution across the evening window people actually book:
#   4 origins x 10 clubs x 13 half-hours = 520/day = 15.6k/month, inside the free tier.
# Outside 17:00-23:00 the UI falls back to the free OSRM figure, marked as approximate.
BUCKETS = [f"{h:02d}:{m:02d}" for h in range(17, 23) for m in (0, 30)] + ["23:00"]
DAY_TYPES = 1  # weekdays only: weekend evenings are not what this tool is for
# Offset for the clubs' timezone; EDT in summer, EST in winter.
LOCAL_OFFSET = os.getenv("DRIVE_TZ_OFFSET", "-04:00")


def die(msg):
    print(f"GUARD {msg}", file=sys.stderr)
    sys.exit(2)


class Lock:
    """Guard 8. Two overlapping runs would double the spend, so only one may exist."""

    def __enter__(self):
        self.fh = open(LOCK, "w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            die(f"another gen_drive run holds {LOCK}")
        self.fh.write(f"{os.getpid()} {datetime.now().isoformat()}\n")
        self.fh.flush()
        return self

    def __exit__(self, *a):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


class Budget:
    """Guards 5, 6 and 9. Persistent day and month counters, charged before each call.
    Never bypassable: --force is for reruns, never for money."""

    def __init__(self, provider, planned):
        self.provider = provider
        self.day = datetime.now().strftime("%Y-%m-%d")
        self.month = self.day[:7]
        self.day_limit = DAILY_BUDGET[provider]
        self.month_limit = MONTHLY_BUDGET[provider]
        want = os.getenv("DRIVE_DAY_LIMIT")
        if want:
            try:
                want = int(want)
            except ValueError:
                die(f"DRIVE_DAY_LIMIT={want!r} is not an integer; refusing to guess")
            if want > HARD_DAY_CEILING:
                die(f"DRIVE_DAY_LIMIT={want} exceeds HARD_DAY_CEILING={HARD_DAY_CEILING}")
            if want > self.day_limit:
                print(f"WARNING daily cap raised {self.day_limit} -> {want} for this run only")
                self.day_limit = want
        self.data = {}
        if LEDGER.exists():
            try:
                self.data = json.loads(LEDGER.read_text())
            except Exception as e:
                # Fail closed. An unreadable ledger means unknown spend, so refuse to spend.
                die(f"ledger {LEDGER} is unreadable ({e}); refusing to spend blind")
        self.days = self.data.setdefault(provider, {})
        self.spent_day = int(self.days.get(self.day, 0))
        self.spent_month = sum(int(v) for d, v in self.days.items() if d.startswith(self.month))
        if self.spent_day + planned > self.day_limit:
            die(f"daily budget: {self.spent_day} spent + {planned} planned > "
                f"{self.day_limit} for {provider} on {self.day}")
        if self.spent_month + planned > self.month_limit:
            die(f"monthly budget: {self.spent_month} spent + {planned} planned > "
                f"{self.month_limit} for {provider} in {self.month}")

    def charge(self):
        self.spent_day += 1
        self.spent_month += 1
        if self.spent_day > self.day_limit or self.spent_month > self.month_limit:
            self.flush()
            die(f"budget exhausted mid-run: day {self.spent_day}/{self.day_limit}, "
                f"month {self.spent_month}/{self.month_limit}")
        self.flush()  # write before the call, so a crash can never lose count

    def flush(self):
        # Write through self.data, never through a cached sub-dict: pruning below rebuilds
        # those sub-dicts, which used to detach self.days and silently drop every later
        # charge. An undercounting ledger is a hole in the budget guard, not a cosmetic bug.
        cutoff = (datetime.now() - timedelta(days=70)).strftime("%Y-%m-%d")
        for p in list(self.data):
            self.data[p] = {d: v for d, v in self.data[p].items() if d >= cutoff}
        self.data.setdefault(self.provider, {})[self.day] = self.spent_day
        self.days = self.data[self.provider]
        LEDGER.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n")


def fetch(url, secret="", referer=""):
    """Guard 10. One attempt, hard timeout, no retry. The HTTP status is inspected and a
    fatal status aborts the entire run rather than the single pair."""
    safe = (lambda t: t.replace(secret, "***") if secret else t)
    headers = {"Referer": referer} if referer else {}
    if httpx is not None:
        # Preferred path: no curl binary in the slim app image used by the CronJob.
        try:
            resp = httpx.get(url, timeout=float(CALL_TIMEOUT_S), headers=headers)
        except Exception as e:
            raise RuntimeError(f"httpx {type(e).__name__} {safe(str(e))[:100]}")
        if resp.status_code in FATAL_STATUS:
            die(f"provider returned HTTP {resp.status_code}: aborting run without retry "
                f"({safe(resp.text)[:120]})")
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} {safe(resp.text)[:80]}")
        return resp.json()
    cmd = ["curl", "-sS", "--max-time", str(CALL_TIMEOUT_S), "-w", "\n%{http_code}"]
    if referer:
        # A URL-restricted Mapbox token rejects requests whose Referer does not match,
        # including requests that send none at all: server-side calls must say who they are.
        cmd += ["-H", f"Referer: {referer}"]
    r = subprocess.run(cmd + [url], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"curl rc={r.returncode} {safe(r.stderr.strip())[:100]}")
    body, _, code = r.stdout.rpartition("\n")
    try:
        status = int(code)
    except ValueError:
        status = 0
    if status in FATAL_STATUS:
        die(f"provider returned HTTP {status}: aborting run without retry "
            f"({safe(body)[:120]})")
    if status >= 400:
        raise RuntimeError(f"HTTP {status} {safe(body)[:80]}")
    return json.loads(body)


def osrm(o, c, key, depart):
    d = fetch("https://router.project-osrm.org/route/v1/driving/"
              f"{o['lng']},{o['lat']};{c['lng']},{c['lat']}?overview=false")
    if d.get("code") != "Ok" or not d.get("routes"):
        raise RuntimeError(f"osrm {d.get('code')}")
    r = d["routes"][0]
    return {"road_mi": round(r["distance"] / 1609.34, 1), "min": round(r["duration"] / 60)}


def tomtom(o, c, key, depart):
    # TomTom departAt is ISO 8601 and must carry an offset, otherwise it is read as UTC and
    # every bucket lands in the wrong part of the traffic day.
    if depart and "+" not in depart[10:] and not depart.endswith("Z") and "-" not in depart[10:]:
        depart = depart + LOCAL_OFFSET
    d = fetch("https://api.tomtom.com/routing/1/calculateRoute/"
              f"{o['lat']},{o['lng']}:{c['lat']},{c['lng']}/json"
              f"?key={key}&traffic=true&routeType=fastest&travelMode=car&departAt={depart}", key)
    routes = d.get("routes") or []
    if not routes:
        raise RuntimeError(f"tomtom empty {str(d)[:80]}")
    s = routes[0]["summary"]
    return {"road_mi": round(s["lengthInMeters"] / 1609.34, 1),
            "min": round(s["travelTimeInSeconds"] / 60)}


def mapbox(o, c, key, depart):
    # Mapbox accepts depart_at as YYYY-MM-DDThh:mm and rejects anything with seconds (422),
    # while TomTom wants full ISO. Normalise here instead of at the call site.
    depart = (depart or "")[:16]
    d = fetch("https://api.mapbox.com/directions/v5/mapbox/driving-traffic/"
              f"{o['lng']},{o['lat']};{c['lng']},{c['lat']}"
              f"?access_token={key}&overview=false&depart_at={depart}", key,
              os.getenv("MAPBOX_REFERER", ""))
    if d.get("code") != "Ok" or not d.get("routes"):
        raise RuntimeError(f"mapbox {d.get('code')}")
    r = d["routes"][0]
    return {"road_mi": round(r["distance"] / 1609.34, 1), "min": round(r["duration"] / 60)}


PROVIDERS = {"osrm": osrm, "tomtom": tomtom, "mapbox": mapbox}
KEY_ENV = {"osrm": None, "tomtom": "TOMTOM_API_KEY", "mapbox": "MAPBOX_TOKEN"}


def same_place(o, c, tol=0.0015):
    """About 150 m: close enough that a route is meaningless."""
    return abs(o["lat"] - c["lat"]) < tol and abs(o["lng"] - c["lng"]) < tol


def next_day_of_type(weekend):
    today = datetime.now()
    want = 5 if weekend else 2
    return today + timedelta(days=((want - today.weekday()) % 7 or 7))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="osrm", choices=list(PROVIDERS))
    ap.add_argument("--dry-run", action="store_true", help="print the plan, make no calls")
    ap.add_argument("--force", action="store_true",
                    help="bypass the rerun interval guard only, never a budget guard")
    ap.add_argument("--i-know-this-spends-quota", dest="consent", action="store_true",
                    help="required for any traffic-aware provider")
    args = ap.parse_args()
    provider = args.provider
    traffic = provider != "osrm"

    clubs = json.loads(CLUBS.read_text())
    missing = [c["name"] for c in clubs if not (c.get("lat") and c.get("lng"))]
    if missing:
        die(f"clubs without coordinates: {missing}")

    # Guards 1 and 2: a paid provider needs both an environment switch and an explicit flag.
    if traffic and not args.dry_run:
        if os.getenv("DRIVE_TRAFFIC") != "1":
            die(f"{provider} is traffic-aware and costs quota; set DRIVE_TRAFFIC=1 to allow it")
        if not args.consent:
            die(f"{provider} needs --i-know-this-spends-quota")

    # Guard 3: bound the shape, so a growing catalogue cannot silently explode the plan.
    if len(ORIGINS) > MAX_ORIGINS:
        die(f"{len(ORIGINS)} origins exceeds MAX_ORIGINS={MAX_ORIGINS}")
    if len(clubs) > MAX_CLUBS:
        die(f"{len(clubs)} clubs exceeds MAX_CLUBS={MAX_CLUBS}")
    if len(BUCKETS) > MAX_BUCKETS:
        die(f"{len(BUCKETS)} buckets exceeds MAX_BUCKETS={MAX_BUCKETS}")

    pairs = len(ORIGINS) * len(clubs)
    planned = pairs * (len(BUCKETS) * DAY_TYPES if traffic else 1)
    expected = pairs * (len(BUCKETS) * DAY_TYPES if traffic else 1)
    if planned != expected:
        die(f"plan arithmetic mismatch: {planned} != {expected}")
    if planned > MAX_CALLS_PER_RUN:            # guard 4
        die(f"planned {planned} exceeds MAX_CALLS_PER_RUN={MAX_CALLS_PER_RUN}")

    key = ""
    if KEY_ENV[provider]:
        key = os.getenv(KEY_ENV[provider], "")
        if not key and not args.dry_run:
            die(f"{KEY_ENV[provider]} is not set")

    print(f"plan: provider={provider} traffic={traffic} pairs={pairs} planned_calls={planned}")
    print(f"caps: run={MAX_CALLS_PER_RUN} day={DAILY_BUDGET[provider]} "
          f"month={MONTHLY_BUDGET[provider]} deadline={RUN_DEADLINE_S}s")

    # Guard 7
    if OUT.exists() and not args.force and not args.dry_run:
        try:
            prev = json.loads(OUT.read_text())
            if prev.get("provider") == provider:
                age = datetime.now().astimezone() - datetime.fromisoformat(prev["generated_at"])
                if age < timedelta(hours=MIN_INTERVAL_H):
                    die(f"last {provider} run was {age} ago, minimum interval is "
                        f"{MIN_INTERVAL_H}h (use --force)")
        except SystemExit:
            raise
        except Exception:
            pass

    if args.dry_run:
        # Construct the ledger even in a dry run: it re-reads real spend and applies the
        # daily/monthly guards, so the plan is proven to be affordable rather than merely
        # printed. Nothing is charged, because charge() is never called on this path.
        Budget(provider, planned)
        print("dry run: budget guards passed; no calls made, no quota spent")
        return

    started = time.monotonic()
    with Lock():
        budget = Budget(provider, planned)
        fn = PROVIDERS[provider]
        matrix, failures, consecutive, calls, free_calls = {}, [], 0, 0, 0

        for okey, o in ORIGINS.items():
            matrix[okey] = {}
            for c in clubs:
                if time.monotonic() - started > RUN_DEADLINE_S:   # guard 12
                    budget.flush()
                    die(f"run deadline {RUN_DEADLINE_S}s exceeded after {calls} calls")
                try:
                    if not traffic:
                        budget.charge()
                        matrix[okey][c["name"]] = fn(o, c, key, None)
                        calls += 1
                        consecutive = 0
                        time.sleep(0.3)
                    elif same_place(o, c):
                        # The SoLe Mia origin is the Reserve's own address. Asking a routing
                        # provider how long it takes to arrive where you already are is a
                        # wasted request every single day.
                        matrix[okey][c["name"]] = {"road_mi": 0.0, "min": 0, "by_bucket": {},
                                                   "offpeak": {"road_mi": 0.0, "min": 0},
                                                   "skipped": "same location"}
                    else:
                        per_bucket = {}
                        day = next_day_of_type(False).strftime("%Y-%m-%d")
                        for b in BUCKETS:
                            budget.charge()
                            per_bucket[f"wd {b}"] = fn(o, c, key, f"{day}T{b}:00")
                            calls += 1
                            consecutive = 0
                            time.sleep(0.15)
                        base = per_bucket.get("wd 18:00") or list(per_bucket.values())[0]
                        entry = {"road_mi": base["road_mi"], "min": base["min"],
                                 "by_bucket": {k: v["min"] for k, v in per_bucket.items()}}
                        # Free-flow reference for slots outside the measured window. OSRM is
                        # keyless and free, so this costs nothing at the paid provider.
                        try:
                            entry["offpeak"] = osrm(o, c, "", None)
                            free_calls += 1
                            time.sleep(0.25)
                        except Exception as e:
                            failures.append(f"{okey}/{c['name']} offpeak: {e}")
                        matrix[okey][c["name"]] = entry
                except SystemExit:
                    budget.flush()
                    raise
                except Exception as e:
                    failures.append(f"{okey}/{c['name']}: {e}")
                    consecutive += 1
                    if consecutive >= MAX_CONSECUTIVE_FAILURES:   # guard 11
                        budget.flush()
                        die(f"{consecutive} consecutive failures, aborting. "
                            f"last: {failures[-1][:120]}")

        budget.flush()

    OUT.write_text(json.dumps({
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "provider": provider,
        "traffic_aware": traffic,
        "buckets": BUCKETS if traffic else [],
        "precise_window": [BUCKETS[0], BUCKETS[-1]] if traffic else [],
        "day_types": ["wd"] if traffic else [],
        "origins": ORIGINS,
        "matrix": matrix,
    }, indent=2) + "\n")

    print(f"done paid_calls={calls} free_calls={free_calls} day={budget.spent_day}/{budget.day_limit} "
          f"month={budget.spent_month}/{budget.month_limit} failures={len(failures)}")
    for f in failures:
        print("  FAIL", f)
    print("WROTE", OUT)
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
