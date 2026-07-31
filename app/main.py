"""padel-poc API: aggregates padel court availability for the Aventura<->Hollywood area.

Single pod: FastAPI serves both the JSON API (/api/*) and the static UI (/).
Upstream is Playtomic's public availability endpoint. If egress is blocked or the
upstream shape changes, the API degrades to a bundled snapshot instead of failing.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date as date_cls
from pathlib import Path
from typing import Any

import base64
import gzip
import pathlib

import httpx
from fastapi import Body, FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

LOG = logging.getLogger("padel")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

BASE_DIR = Path(__file__).parent
SNAPSHOT = BASE_DIR / "data" / "snapshot.json"
CLUBS_FILE = Path(os.getenv("CLUBS_FILE", BASE_DIR / "data" / "clubs.json"))
PLAYTOMIC_API = os.getenv("PLAYTOMIC_API", "https://api.playtomic.io/v1")
PLAYTOMIC_AUTH = os.getenv("PLAYTOMIC_AUTH", "https://api.playtomic.io/v3/auth/login")
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "600"))
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT_SECONDS", "8"))

# Credentials come from a Kubernetes Secret; they are never logged or persisted.
PLAYTOMIC_EMAIL = os.getenv("PLAYTOMIC_EMAIL", "")
PLAYTOMIC_PASSWORD = os.getenv("PLAYTOMIC_PASSWORD", "")

# Direct upstream calls are disabled by default: api.playtomic.io is behind
# CloudFront and returns 403 to any non-browser client. The scraper CronJob
# (headless Chromium) POSTs data to /api/ingest instead.
DIRECT_FETCH = os.getenv("DIRECT_FETCH", "false").lower() == "true"
INGEST_TTL = int(os.getenv("INGEST_TTL_SECONDS", "5400"))

app = FastAPI(title="Padel Finder", version=os.getenv("APP_VERSION", "0.1.0"))
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
# date -> (received_at, slots)
_ingested: dict[str, tuple[float, list[dict[str, Any]]]] = {}
# audit trail of the last scraper run
_last_scan: dict[str, Any] = {}
_token: dict[str, Any] = {"access": "", "obtained_at": 0.0}
TOKEN_TTL = int(os.getenv("TOKEN_TTL_SECONDS", "3000"))



# --------------------------------------------------------------------------
# ConfigMap-backed cache: the pod keeps slots in memory, but a restart must not
# drop them back to the bundled snapshot. Payload is gzipped into binaryData
# because a ConfigMap is capped at 1 MiB and a full scan is ~700 KB of JSON.
# --------------------------------------------------------------------------
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
K8S_HOST = os.getenv("KUBERNETES_HOST", "https://kubernetes.default.svc")
CACHE_CM = os.getenv("CACHE_CONFIGMAP", "")
POD_NS = os.getenv("POD_NAMESPACE", "")
CACHE_KEY = "cache.json.gz"
SCRAPER_CRONJOB = os.getenv("SCRAPER_CRONJOB", "padel-scraper")
SCRAPER_SVC = os.getenv("SCRAPER_SERVICE", "http://padel-scraper")
RECHECK_TIMEOUT = float(os.getenv("RECHECK_TIMEOUT_SECONDS", "90"))


def _k8s() -> httpx.Client | None:
    token_file = pathlib.Path(SA_DIR) / "token"
    if not (CACHE_CM and POD_NS and token_file.exists()):
        return None
    return httpx.Client(
        base_url=K8S_HOST,
        verify=f"{SA_DIR}/ca.crt",
        headers={"Authorization": f"Bearer {token_file.read_text().strip()}"},
        timeout=15,
    )


def cache_save() -> None:
    client = _k8s()
    if client is None:
        return
    blob = {
        "saved_at": time.time(),
        "days": {d: {"received_at": ts, "slots": rows} for d, (ts, rows) in _ingested.items()},
        "last_scan": _last_scan,
    }
    packed = base64.b64encode(gzip.compress(json.dumps(blob).encode())).decode()
    body = {"metadata": {"name": CACHE_CM}, "binaryData": {CACHE_KEY: packed}}
    path = f"/api/v1/namespaces/{POD_NS}/configmaps/{CACHE_CM}"
    try:
        with client:
            resp = client.patch(path, json=body,
                                headers={"Content-Type": "application/merge-patch+json"})
            if resp.status_code == 404:
                resp = client.post(f"/api/v1/namespaces/{POD_NS}/configmaps", json=body)
            if resp.status_code >= 300:
                LOG.warning("cache save failed: HTTP %s %s", resp.status_code, resp.text[:200])
            else:
                LOG.info("cache saved to configmap/%s (%s KB gzipped)", CACHE_CM, len(packed) // 1024)
    except Exception as exc:  # noqa: BLE001 - cache is best effort, never fatal
        LOG.warning("cache save error: %s", exc)


def cache_load() -> None:
    client = _k8s()
    if client is None:
        LOG.info("configmap cache disabled")
        return
    try:
        with client:
            resp = client.get(f"/api/v1/namespaces/{POD_NS}/configmaps/{CACHE_CM}")
        if resp.status_code == 404:
            LOG.info("no cache configmap yet - starting cold")
            return
        resp.raise_for_status()
        packed = (resp.json().get("binaryData") or {}).get(CACHE_KEY)
        if not packed:
            return
        blob = json.loads(gzip.decompress(base64.b64decode(packed)))
        for day, entry in (blob.get("days") or {}).items():
            _ingested[day] = (float(entry["received_at"]), entry["slots"])
        _last_scan.clear()
        _last_scan.update(blob.get("last_scan") or {})
        LOG.info("cache restored: %s day(s), %s slots",
                 len(_ingested), sum(len(v[1]) for v in _ingested.values()))
    except Exception as exc:  # noqa: BLE001
        LOG.warning("cache load error: %s", exc)


@app.on_event("startup")
def _restore_cache() -> None:
    cache_load()


def auth_configured() -> bool:
    return bool(PLAYTOMIC_EMAIL and PLAYTOMIC_PASSWORD)


def get_token(client: httpx.Client, force: bool = False) -> str:
    """Return a cached bearer token, logging in when missing/expired.

    Never logs credentials or the token value.
    """
    if not auth_configured():
        return ""
    fresh = _token["access"] and time.time() - _token["obtained_at"] < TOKEN_TTL
    if fresh and not force:
        return str(_token["access"])
    resp = client.post(
        PLAYTOMIC_AUTH,
        json={"email": PLAYTOMIC_EMAIL, "password": PLAYTOMIC_PASSWORD},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    token = body.get("access_token") or body.get("accessToken") or ""
    if not token:
        raise RuntimeError("login response contained no access_token")
    _token["access"] = token
    _token["obtained_at"] = time.time()
    LOG.info("playtomic login ok (token cached, ttl=%ss)", TOKEN_TTL)
    return str(token)


def load_clubs() -> list[dict[str, Any]]:
    with CLUBS_FILE.open() as fh:
        return json.load(fh)


def load_snapshot() -> dict[str, Any]:
    with SNAPSHOT.open() as fh:
        return json.load(fh)


def fetch_club(client: httpx.Client, club: dict[str, Any], day: str) -> list[dict[str, Any]]:
    """Return normalised slots for one club/day. Raises on transport errors."""
    params = {
        "sport_id": "PADEL",
        "tenant_id": club["tenant_id"],
        "local_start_min": f"{day}T00:00:00",
        "local_start_max": f"{day}T23:59:59",
    }
    token = get_token(client)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = client.get(
        f"{PLAYTOMIC_API}/availability", params=params, headers=headers, timeout=HTTP_TIMEOUT
    )
    if resp.status_code in (401, 403) and token:
        # token may have expired mid-flight: re-login once, then retry
        token = get_token(client, force=True)
        resp = client.get(
            f"{PLAYTOMIC_API}/availability",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=HTTP_TIMEOUT,
        )
    resp.raise_for_status()
    out: list[dict[str, Any]] = []
    for resource in resp.json():
        for slot in resource.get("slots", []):
            price_raw = str(slot.get("price", "0")).replace("USD", "").strip()
            try:
                price = float(price_raw)
            except ValueError:
                price = 0.0
            out.append(
                {
                    "club": club["name"],
                    "area": club["area"],
                    "drive": club.get("drive", ""),
                    "indoor": club["indoor"],
                    "start": slot.get("start_time", "")[:5],
                    "duration": slot.get("duration"),
                    "price": round(price, 2),
                    "court": resource.get("resource_id", ""),
                    "url": club["url"],
                }
            )
    return out


def collect(day: str) -> dict[str, Any]:
    # 1. Freshest source: data pushed by the scraper CronJob (headless Chromium).
    pushed = _ingested.get(day)
    if pushed and time.time() - pushed[0] < INGEST_TTL:
        age = int(time.time() - pushed[0])
        return {
            "date": day,
            "source": "playtomic-browser",
            "age_seconds": age,
            "slots": pushed[1],
            "errors": [],
        }

    cached = _cache.get(day)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]

    slots: list[dict[str, Any]] = []
    errors: list[str] = []
    if not (DIRECT_FETCH or auth_configured()):
        snap = load_snapshot()
        return {
            "date": snap["date"],
            "source": "snapshot",
            "slots": snap["slots"],
            "errors": ["direct upstream disabled (CloudFront blocks non-browser clients)"],
        }
    clubs = load_clubs()
    with httpx.Client(headers={"User-Agent": os.getenv("APP_USER_AGENT", "padel-finder/0.1")}) as client:
        for club in clubs:
            if not club.get("tenant_id"):
                errors.append(f"{club['name']}: no tenant_id configured")
                continue
            try:
                slots.extend(fetch_club(client, club, day))
            except Exception as exc:  # noqa: BLE001 - upstream is undocumented
                LOG.warning("upstream failed for %s: %s", club["name"], exc)
                errors.append(f"{club['name']}: {type(exc).__name__}")

    if slots:
        payload = {"date": day, "source": "playtomic", "slots": slots, "errors": errors}
    else:
        snap = load_snapshot()
        payload = {
            "date": snap["date"],
            "source": "snapshot",
            "slots": snap["slots"],
            "errors": errors or ["upstream unreachable"],
        }

    _cache[day] = (time.time(), payload)
    return payload


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": app.version,
        "clubs": len(load_clubs()),
        "auth": "configured" if auth_configured() else "anonymous",
        "cache_configmap": CACHE_CM or None,
        "cached_days": sorted(_ingested.keys()),
        "token_cached": bool(_token["access"]),
    }


@app.post("/api/ingest")
def ingest(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Receive scraped availability from the CronJob.

    This endpoint should be cluster-internal; do not expose it without authentication and request validation.
    Slots carry their own `date`; they are bucketed per day.
    """
    rows = payload.get("slots") or []
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        day = str(row.get("date", ""))[:10]
        if not day:
            continue
        buckets.setdefault(day, []).append(row)
    now = time.time()
    partial = bool(payload.get("partial"))
    prev_scan = dict(_last_scan)
    if partial:
        # Targeted re-check: replace ONLY the rechecked (club, day) pairs.
        # Filtering by club alone used to wipe that club's other days.
        touched_pairs = {(r.get("club"), str(r.get("date", ""))[:10])
                         for r in rows if r.get("club")}
        for day, day_rows in buckets.items():
            prev = _ingested.get(day)
            kept = ([r for r in prev[1]
                     if (r.get("club"), day) not in touched_pairs] if prev else [])
            buckets[day] = kept + day_rows
        payload = dict(payload)
        payload["report"] = [
            r for r in (prev_scan.get("report") or [])
            if (r.get("club"), str(r.get("date", ""))[:10]) not in touched_pairs
        ] + (payload.get("report") or [])
        held = sum(len(v) for v in buckets.values()) + sum(
            len(v[1]) for d, v in _ingested.items() if d not in buckets)
    _last_scan.clear()
    _last_scan.update({
        "source": payload.get("source", "unknown"),
        # A recheck must never masquerade as a full scan: keep the full-scan
        # timestamp and expose the recheck moment separately.
        "scanned_at": prev_scan.get("scanned_at") if partial else payload.get("scanned_at"),
        "received_at": prev_scan.get("received_at", now) if partial else now,
        "last_recheck_at": payload.get("scanned_at") if partial else prev_scan.get("last_recheck_at"),
        "report": payload.get("report", []),
        "errors": prev_scan.get("errors", []) if partial else payload.get("errors", []),
        "clubs": payload.get("clubs") or prev_scan.get("clubs", []),
        "center": payload.get("center") or prev_scan.get("center"),
        "radius_miles": payload.get("radius_miles") or prev_scan.get("radius_miles"),
        # total must reflect everything we hold, not just this payload
        "total_slots": held if partial else len(rows),
    })
    for day, day_rows in buckets.items():
        _ingested[day] = (now, day_rows)
        _cache.pop(day, None)
    LOG.info("ingested %s slots across %s day(s)", len(rows), len(buckets))
    cache_save()
    return {
        "accepted": len(rows),
        "days": {day: len(v) for day, v in sorted(buckets.items())},
        "source": payload.get("source", "unknown"),
    }


@app.post("/api/recheck")
def recheck(club: str = Query(...), day: str = Query(..., alias="date")) -> JSONResponse:
    """Re-scan one club/day right now via the warm scraper service.

    Returns a verdict the UI can show immediately: confirmed (slots still there)
    or cancelled (the slot is gone).
    """
    try:
        with httpx.Client(timeout=RECHECK_TIMEOUT) as client:
            resp = client.post(f"{SCRAPER_SVC}/scrape", params={"club": club, "date": day})
        if resp.status_code == 429:
            return JSONResponse({"error": "scraper busy, retry in a few seconds"}, status_code=429)
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        LOG.warning("recheck failed for %s/%s: %s", club, day, exc)
        return JSONResponse({"error": f"{type(exc).__name__}"}, status_code=502)
    LOG.info("recheck %s / %s -> %s (%s slots)", club, day, body.get("verdict"), body.get("slots"))
    return JSONResponse(body)


@app.get("/api/scan")
def scan_status() -> dict[str, Any]:
    """Audit trail of the last scraper run: club, day, status, slots, when."""
    if not _last_scan:
        return {"scanned": False, "source": "snapshot", "report": [], "errors": [],
                "message": "No live scan received yet - serving the bundled snapshot."}
    body = dict(_last_scan)
    body["scanned"] = True
    body["age_seconds"] = int(time.time() - float(body.pop("received_at", time.time())))
    return body


@app.get("/api/clubs")
def clubs() -> list[dict[str, Any]]:
    """Club catalogue: the static record merged with whatever the last scan learned.

    The static file is authoritative for coordinates/price/address; the scan only
    adds discovered extras (logo, description). Returning the scan payload alone
    used to drop lat/lng and broke client-side distance.
    """
    static = {c["name"]: dict(c) for c in load_clubs()}
    for disc in _last_scan.get("clubs") or []:
        name = disc.get("name")
        if not name:
            continue
        base = static.setdefault(name, {"name": name})
        for k, v in disc.items():
            if k not in base or base[k] in (None, ""):
                base[k] = v
            elif k == "_meta":
                base["_meta"] = v
    return sorted(static.values(), key=lambda c: float(c.get("miles") or 999))


# --- live drive times for an arbitrary origin ---------------------------------------
# The precomputed matrix only covers the four saved origins. Browser geolocation can be
# anywhere, and falling back to straight-line distance is actively misleading: Epic reads
# 14.5 mi straight but is 19.4 mi and 28 min by road. One OSRM table call answers all
# clubs at once, so an arbitrary origin costs exactly one upstream request.
_LIVE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_LIVE_TTL_S = 86400.0
_LIVE_MAX_PER_DAY = 500
_LIVE_SPENT: dict[str, int] = {}
_OSRM_TABLE = "https://router.project-osrm.org/table/v1/driving/"


@app.get("/api/drive/live")
def drive_live(lat: float, lng: float) -> dict[str, Any]:
    """Road distance and drive time from one arbitrary point to every club.

    Guarded like the offline generator: coordinates are rounded to ~110 m to make the
    cache effective, results are cached for a day, and a per-day ceiling keeps a runaway
    client from hammering the public OSRM demo server. Failure is soft: an empty matrix
    means the UI shows straight-line distance and says so.
    """
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return {"provider": None, "matrix": {}, "error": "coordinates out of range"}

    key = f"{round(lat, 3)},{round(lng, 3)}"
    now = time.time()
    hit = _LIVE_CACHE.get(key)
    if hit and now - hit[0] < _LIVE_TTL_S:
        return {"provider": "osrm-table", "cached": True, "matrix": hit[1]}

    day = date_cls.today().isoformat()
    spent = _LIVE_SPENT.get(day, 0)
    if spent >= _LIVE_MAX_PER_DAY:
        return {"provider": None, "matrix": {}, "error": f"daily ceiling {_LIVE_MAX_PER_DAY} reached"}

    clubs = [c for c in load_clubs() if c.get("lat") and c.get("lng")]
    if not clubs:
        return {"provider": None, "matrix": {}}

    coords = ";".join([f"{lng},{lat}"] + [f"{c['lng']},{c['lat']}" for c in clubs])
    url = f"{_OSRM_TABLE}{coords}?sources=0&annotations=duration,distance"
    _LIVE_SPENT[day] = spent + 1  # count before the call, never after
    try:
        data = httpx.get(url, timeout=20.0).json()
    except Exception as exc:  # noqa: BLE001 - soft failure by design
        return {"provider": None, "matrix": {}, "error": str(exc)[:120]}

    if data.get("code") != "Ok":
        return {"provider": None, "matrix": {}, "error": str(data.get("code"))}

    durations = (data.get("durations") or [[]])[0]
    distances = (data.get("distances") or [[]])[0]
    out: dict[str, Any] = {}
    for i, c in enumerate(clubs, start=1):
        if i >= len(durations) or durations[i] is None:
            continue
        meters = distances[i] if i < len(distances) and distances[i] is not None else 0
        out[c["name"]] = {"min": round(durations[i] / 60), "road_mi": round(meters / 1609.34, 1)}

    _LIVE_CACHE[key] = (now, out)
    return {"provider": "osrm-table", "cached": False, "matrix": out}


@app.get("/api/drive")
def drive() -> dict[str, Any]:
    """Precomputed road distance and drive time from each saved origin to each club.

    Generated offline by scripts/gen_drive.py, so serving it costs nothing and no routing
    provider is called per request. Absent file is not an error: the UI falls back to
    straight-line distance.
    """
    # The CronJob publishes a fresh matrix into a ConfigMap mounted at DRIVE_FILE. The copy
    # baked into the image is the fallback, so the app still works before the first run and
    # if the ConfigMap is ever empty.
    override = os.getenv("DRIVE_FILE")
    if override:
        p = Path(override)
        if p.exists() and p.stat().st_size > 2:
            try:
                data = json.loads(p.read_text())
                if data.get("matrix"):
                    return data
            except Exception:  # noqa: BLE001 - fall through to the baked-in copy
                pass
    path = CLUBS_FILE.parent / "drive.json"
    if not path.exists():
        return {"provider": None, "traffic_aware": False, "matrix": {}}
    try:
        return json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001 - a corrupt file must not take the API down
        return {"provider": None, "traffic_aware": False, "matrix": {}, "error": str(exc)}


@app.get("/api/slots")
def api_slots(
    day: str = Query(default_factory=lambda: date_cls.today().isoformat(), alias="date"),
    indoor: bool | None = None,
    duration: int | None = None,
    start_from: str | None = None,
    start_to: str | None = None,
    court_type: str | None = None,
    group: bool = True,
) -> JSONResponse:
    payload = collect(day)
    rows = payload["slots"]
    # The cache can still hold rows for clubs that were removed from the
    # catalogue; never serve a club we no longer claim to cover.
    known = {c["name"] for c in load_clubs()}
    rows = [r for r in rows if r["club"] in known]
    if court_type:
        rows = [r for r in rows if (r.get("court_type") or "") == court_type]
    if indoor is not None:
        rows = [r for r in rows if bool(r["indoor"]) is indoor]
    if duration is not None:
        rows = [r for r in rows if r["duration"] == duration]
    if start_from:
        rows = [r for r in rows if r["start"] >= start_from]
    if start_to:
        rows = [r for r in rows if r["start"] < start_to]
    if group:
        merged: dict[tuple, dict[str, Any]] = {}
        for r in rows:
            key = (r["club"], r["start"], r["duration"])
            hit = merged.get(key)
            if hit is None:
                hit = dict(r)
                hit["courts"] = []
                merged[key] = hit
            if r.get("court") and r["court"] not in hit["courts"]:
                hit["courts"].append(r["court"])
            ct = r.get("court_type") or ""
            types = hit.setdefault("court_types", [])
            if ct and ct not in types:
                types.append(ct)
            if isinstance(r.get("price"), (int, float)):
                cur = hit.get("price")
                hit["price"] = r["price"] if not isinstance(cur, (int, float)) else min(cur, r["price"])
                lo, hi = hit.get("price_min"), hit.get("price_max")
                hit["price_min"] = r["price"] if not isinstance(lo, (int, float)) else min(lo, r["price"])
                hit["price_max"] = r["price"] if not isinstance(hi, (int, float)) else max(hi, r["price"])
                hit.setdefault("court_prices", {})[r.get("court") or ""] = r["price"]
        rows = []
        for hit in merged.values():
            hit["court_count"] = len(hit["courts"]) or 1
            hit["court"] = ", ".join(hit["courts"]) if hit["courts"] else hit.get("court", "")
            rows.append(hit)
    rows = sorted(rows, key=lambda r: (r["start"], -(r.get("court_count") or 1)))
    body = dict(payload)
    body["slots"] = rows
    body["count"] = len(rows)
    priced = [r["price"] for r in rows if isinstance(r.get("price"), (int, float))]
    body["cheapest"] = min(priced, default=None)
    body["priced_slots"] = len(priced)
    return JSONResponse(body)


app.mount("/", StaticFiles(directory=str(BASE_DIR / "static"), html=True), name="ui")
