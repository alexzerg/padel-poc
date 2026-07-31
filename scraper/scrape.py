"""Playwright scraper for Playtomic.

Two jobs:
  1. discover every padel club within RADIUS_MILES of CENTER_COORD (default: ZIP 33020);
  2. pull availability for the next DAYS_AHEAD days for each of them.

Why a browser: api.playtomic.io sits behind CloudFront, which answers 403 to any
non-browser client (verified with curl/httpx + full browser headers, on both
/v1/availability and /v3/auth/login). Running fetch() from inside a real Chromium
on the playtomic.com origin passes. No credentials are used - public data only.
"""
from __future__ import annotations

import json
import math
import os
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone

from playwright.sync_api import sync_playwright

API_INGEST = os.getenv("API_INGEST", "http://padel/api/ingest")
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "3"))
# ZIP 33020 = Hollywood, FL
CENTER_COORD = os.getenv("CENTER_COORD", "26.0112,-80.1495")
RADIUS_MILES = float(os.getenv("RADIUS_MILES", "20"))
NAV_TIMEOUT = int(os.getenv("NAV_TIMEOUT_MS", "20000"))
# targeted re-check: comma separated club names / dates
CLUBS_ONLY = [x.strip().lower() for x in os.getenv("CLUBS_ONLY", "").split(",") if x.strip()]
DAYS_ONLY = [x.strip() for x in os.getenv("DAYS_ONLY", "").split(",") if x.strip()]
OVERRIDES_FILE = os.getenv("OVERRIDES_FILE", "/srv/app/data/clubs.json")

MILES_PER_METER = 1 / 1609.34

API_BASE = "https://api.playtomic.io/v1"
BROWSER_HEADERS = {
    "Accept": "application/json",
    "Referer": "https://playtomic.com/",
    "Origin": "https://playtomic.com",
}


def api_get(ctx, page, url: str):
    """GET JSON through the browser network stack.

    context.request keeps the browser TLS fingerprint and cookies but is not
    subject to page CORS policy (an in-page fetch() to api.playtomic.io fails
    CORS). If that is refused, fall back to a plain navigation, which CloudFront
    treats as a normal browser request, and read the JSON out of the body.
    """
    resp = ctx.request.get(url, headers=BROWSER_HEADERS, timeout=NAV_TIMEOUT)
    if resp.ok:
        return resp.status, resp.json()
    status = resp.status
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        text = page.evaluate("() => document.body ? document.body.innerText : ''")
        return 200, json.loads(text)
    except Exception:  # noqa: BLE001
        return status, []


def discover(ctx, page, lat: str, lng: str, radius_m: int):
    items = []
    last_status = 0
    for page_no in range(6):
        url = (f"{API_BASE}/tenants?coordinate={lat},{lng}&radius={radius_m}"
               f"&sport_id=PADEL&playtomic_status=ACTIVE&size=40&page={page_no}")
        last_status, body = api_get(ctx, page, url)
        if not isinstance(body, list) or not body:
            break
        items.extend(body)
        if len(body) < 40:
            break
    return last_status, items


def availability(ctx, page, tenant_id: str, day: str):
    url = (f"{API_BASE}/availability?sport_id=PADEL&tenant_id={tenant_id}"
           f"&local_start_min={day}T00:00:00&local_start_max={day}T23:59:59")
    return api_get(ctx, page, url)



META_JS = """
() => {
  const abs = (u) => {
    if (!u) { return null; }
    try { return new URL(u, location.href).href; } catch (e) { return null; }
  };
  const attr = (sel, name) => {
    const el = document.querySelector(sel);
    return el ? el.getAttribute(name) : null;
  };
  // og:image is missing on some club pages, so walk a fallback chain and finally
  // pick the largest rendered <img> that is not an icon or a tracking pixel.
  let img = abs(attr('meta[property="og:image"]', 'content'))
    || abs(attr('meta[name="twitter:image"]', 'content'))
    || abs(attr('link[rel="image_src"]', 'href'));
  if (!img) {
    const cands = Array.from(document.images)
      .filter((i) => i.currentSrc && i.naturalWidth >= 200 && i.naturalHeight >= 120)
      .filter((i) => !/sprite|icon|favicon|logo-playtomic|pixel/i.test(i.currentSrc))
      .sort((a, b) => (b.naturalWidth * b.naturalHeight) - (a.naturalWidth * a.naturalHeight));
    if (cands.length) { img = cands[0].currentSrc; }
  }
  const desc = attr('meta[name="description"]', 'content');
  const maps = attr('[href*="google.com/maps"], [href^="https://maps.google"], [href^="https://maps.app"]', 'href');
  const h1 = document.querySelector('h1');
  return {
    logo: img,
    title: h1 ? h1.textContent.trim() : null,
    description: desc,
    maps_url: maps,
  };
}
"""

EXTRACT_JS = """
() => {
  // Playtomic renders one row per court: the fixed-width header cell and that
  // court's slot cells are siblings inside the same .border-b row. Reading the
  // court from each slot's own row is exact. The previous index-based mapping
  // broke whenever a court had no free slots, because such courts never appear
  // among the slot ids and every later court shifted by one.
  const readCourt = (head) => {
    // Some clubs (One Indoor Club) render the header without .truncate, which
    // used to yield an empty name -> the whole court row was skipped and every
    // one of its cells ended up orphaned. Fall back to parsing the header text,
    // which looks like 'Padel 1Padel 1indoor, double, panoramic' (label twice).
    let name = (head.querySelector('.truncate')?.textContent || '').trim();
    const raw = (head.textContent || '').replace(/\s+/g, ' ').trim();
    const pm = raw.match(/(indoor|outdoor)\s*,/i);
    let props = pm ? raw.slice(pm.index).trim() : '';
    if (!props) {
      const divs = Array.from(head.querySelectorAll('div'))
        .map((d) => (d.textContent || '').trim()).filter(Boolean);
      for (const t of divs) {
        if (t !== name && t.length < 80 && /(indoor|outdoor)/i.test(t)) {
          props = t.startsWith(name) ? t.slice(name.length).trim() : t;
          break;
        }
      }
    }
    if (!name) {
      let label = (pm ? raw.slice(0, pm.index) : raw).trim();
      const half = label.length / 2;
      if (label.length > 1 && label.length % 2 === 0 &&
          label.slice(0, half) === label.slice(half)) {
        label = label.slice(0, half);
      }
      name = label.trim();
    }
    if (name && props.startsWith(name)) { props = props.slice(name.length).trim(); }
    return { name: name, props: props };
  };

  const readCell = (el, court) => {
    const id = el.getAttribute('data-slot-id') || '';
    return {
      slot_id: id,
      resource_id: id.slice(0, 36),
      time: el.getAttribute('data-tracking-property-time') || '',
      duration: parseInt(el.getAttribute('data-tracking-property-duration') || '0', 10),
      court: court.name,
      props: court.props,
    };
  };

  const courts = [];
  const slots = [];
  const order = [];
  const seen = new Set();

  Array.from(document.querySelectorAll('[style*="width:150px"]')).forEach((head) => {
    const court = readCourt(head);
    if (!court.name) { return; }
    courts.push(court);
    const row = head.closest('.border-b');
    if (!row) { return; }
    Array.from(row.querySelectorAll('[data-slot-id]')).forEach((el) => {
      const id = el.getAttribute('data-slot-id') || '';
      if (seen.has(id)) { return; }
      seen.add(id);
      const rid = id.slice(0, 36);
      if (!order.includes(rid)) { order.push(rid); }
      slots.push(readCell(el, court));
    });
  });

  // Safety net: never silently drop a slot whose row could not be resolved.
  let orphans = 0;
  Array.from(document.querySelectorAll('[data-slot-id]')).forEach((el) => {
    const id = el.getAttribute('data-slot-id') || '';
    if (seen.has(id)) { return; }
    seen.add(id);
    orphans += 1;
    const rid = id.slice(0, 36);
    if (!order.includes(rid)) { order.push(rid); }
    slots.push(readCell(el, { name: '', props: '' }));
  });

  const dateEl = document.querySelector('input[type=date]');
  return {
    page_date: dateEl ? dateEl.value : null,
    resource_order: order,
    courts: courts,
    slots: slots,
    orphan_cells: orphans,
    total_cells: document.querySelectorAll('[data-slot-id]').length,
  };
}
"""


# Playtomic renders the same availability twice: a desktop grid of cells, and a
# mobile accordion with one <details> per court. The accordion is strictly
# better as a data source - the court is the container (so a slot can never be
# attributed to the wrong court, and nothing can be orphaned), and every option
# carries its real price. The grid stays as a fallback.
ACCORDION_JS = """
() => {
  const rowRe = /^(\d{1,2}:\d{2}\s*[AP]M)\s*-\s*(\d{1,2}:\d{2}\s*[AP]M)\s*.\s*(\d+)\s*min\s*.\s*\$?([\d,.]+)/;
  const top = Array.from(document.querySelectorAll('details'))
    .filter(d => !d.parentElement || !d.parentElement.closest('details'));
  const courts = [];
  for (const d of top) {
    const sum = d.querySelector('summary');
    if (!sum) continue;
    const nameEl = sum.querySelector('div.flex-1 > div');
    const court = nameEl ? (nameEl.textContent || '').trim() : '';
    if (!court) continue;
    const seen = new Set();
    const options = [];
    for (const x of d.querySelectorAll('div')) {
      const t = (x.textContent || '').replace(/\u00a0/g, ' ').trim();
      const m = t.match(rowRe);
      if (!m) continue;
      const key = m[1] + '|' + m[3];
      if (seen.has(key)) continue;
      seen.add(key);
      options.push({ start: m[1], end: m[2], duration: parseInt(m[3], 10),
                     price: parseFloat(m[4].replace(/,/g, '')) });
    }
    if (options.length) courts.push({ court: court, options: options });
  }
  return { courts: courts };
}
"""

KEEP_DURATIONS = {int(x) for x in os.getenv("DURATIONS", "60,90").split(",") if x.strip()}


def _minutes(hhmm: str) -> int | None:
    try:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    except Exception:  # noqa: BLE001
        return None


def audit_contiguity(slots: list[dict]) -> tuple[dict[str, set[int]], int]:
    """Playtomic publishes one cell per (start, duration) option. A 60 min option
    is only real if that court is also free for both 30 min halves. Cross-check
    the club's own numbers instead of trusting them, and count the mismatches.
    """
    free30: dict[str, set[int]] = {}
    for s in slots:
        if s.get("duration") == 30:
            m = _minutes(to_24h(s.get("time", "")))
            if m is not None:
                free30.setdefault(s.get("court") or "", set()).add(m)
    bad = 0
    for s in slots:
        dur = s.get("duration") or 0
        if dur <= 30:
            continue
        m = _minutes(to_24h(s.get("time", "")))
        if m is None:
            continue
        have = free30.get(s.get("court") or "", set())
        if not have:
            continue
        if any((m + step) not in have for step in range(0, dur, 30)):
            bad += 1
    return free30, bad


def court_facts(court: dict | None, club_default: bool) -> dict:
    """Turn a scraped court header into facts about that single court.

    Playtomic states "indoor, double, panoramic" per court in the column tooltip,
    so indoor/outdoor is ground truth per court. The club-level flag is only used
    when a court has no properties at all, and is labelled as such.
    """
    props = ((court or {}).get("props") or "").lower()
    known = "indoor" in props or "outdoor" in props
    return {
        "court": (court or {}).get("court") or (court or {}).get("name") or "",
        "indoor": ("indoor" in props) if known else bool(club_default),
        "indoor_source": ("court-props" if known else
                          ("unknown" if club_default is None else "club-default")),
        "court_type": "single" if "single" in props else ("double" if "double" in props else ""),
        "wall": "panoramic" if "panoramic" in props else ("crystal" if "crystal" in props else ""),
        "props": props,
    }


def to_24h(label: str) -> str:
    """'6:30 PM' -> '18:30'."""
    try:
        return datetime.strptime(label.strip().upper(), "%I:%M %p").strftime("%H:%M")
    except ValueError:
        return label.strip()[:5]


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)), 1)


def load_overrides() -> dict[str, dict]:
    """Manual hints (indoor flag, nicer area label) keyed by lowercased club name."""
    try:
        with open(OVERRIDES_FILE) as fh:
            return {c["name"].lower(): c for c in json.load(fh)}
    except Exception:  # noqa: BLE001
        return {}


def tenant_to_club(t: dict, center: tuple[float, float], overrides: dict[str, dict]) -> dict:
    addr = t.get("address") or {}
    lat = float(addr.get("coordinate", {}).get("lat") or 0)
    lng = float(addr.get("coordinate", {}).get("lon") or addr.get("coordinate", {}).get("lng") or 0)
    resources = t.get("resources") or []
    feats = []
    for r in resources:
        props = r.get("properties") or {}
        feats.append(str(props.get("resource_feature", "")).lower())
    indoor_courts = sum(1 for f in feats if "indoor" in f)
    name = t.get("tenant_name") or t.get("name") or "unknown"
    ov = overrides.get(name.lower(), {})
    slug = t.get("playtomic_slug") or t.get("slug") or ""
    club = {
        "name": name,
        "tenant_id": t.get("tenant_id") or t.get("id"),
        "area": ov.get("area") or ", ".join(x for x in [addr.get("city"), addr.get("postal_code")] if x),
        "drive": ov.get("drive", ""),
        "indoor": bool(indoor_courts) or bool(ov.get("indoor", False)),
        "indoor_courts": indoor_courts,
        "total_courts": len(resources),
        "miles": haversine_miles(center[0], center[1], lat, lng) if lat else None,
        "url": f"https://playtomic.com/clubs/{slug}" if slug else (ov.get("url") or "https://playtomic.com"),
    }
    return club


def goto_retry(page, url, attempts=3, label="nav"):
    """Navigate with backoff. Cluster networking occasionally throws transient
    net::ERR_NETWORK_CHANGED / ERR_CONNECTION_RESET on the very first hop."""
    delays = [2000, 5000, 10000]
    last = None
    for i in range(attempts):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            if i:
                print(f"{label}: recovered on attempt {i + 1}", flush=True)
            return
        except Exception as exc:  # noqa: BLE001 - retry any nav failure
            last = exc
            msg = str(exc).split("\n")[0]
            print(f"{label}: attempt {i + 1}/{attempts} failed: {msg}", flush=True)
            if i < attempts - 1:
                page.wait_for_timeout(delays[min(i, len(delays) - 1)])
    raise last


def scrape(clubs_only: list[str] | None = None, days_only: list[str] | None = None) -> dict:
    lat_s, lng_s = CENTER_COORD.split(",")
    center = (float(lat_s), float(lng_s))
    radius_m = int(RADIUS_MILES / MILES_PER_METER)
    only_clubs = [c.lower() for c in (clubs_only or CLUBS_ONLY)]
    days = days_only or DAYS_ONLY or [(date.today() + timedelta(days=i)).isoformat() for i in range(DAYS_AHEAD)]
    with open(OVERRIDES_FILE) as fh:
        seeded = json.load(fh)

    all_rows: list[dict] = []
    report: list[dict] = []
    errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(locale="en-US", timezone_id="America/New_York",
                                 viewport={"width": 1440, "height": 900})
        # Every datum we need is DOM text, so images, fonts, media and trackers are
        # pure latency. og:image still resolves because meta tags are markup, not requests.
        if os.getenv("BLOCK_ASSETS", "1") == "1":
            _BLOCKED_TYPES = {"image", "media", "font"}
            _BLOCKED_HOSTS = ("google-analytics.com", "googletagmanager.com", "doubleclick.net",
                              "facebook.net", "hotjar.com", "segment.io", "intercom.io",
                              "sentry.io", "clarity.ms", "amplitude.com")

            def _gate(route):
                req = route.request
                if req.resource_type in _BLOCKED_TYPES or any(h in req.url for h in _BLOCKED_HOSTS):
                    return route.abort()
                return route.continue_()

            ctx.route("**/*", _gate)

        page = ctx.new_page()
        goto_retry(page, "https://playtomic.com/", attempts=4, label="warmup")
        page.wait_for_timeout(800)

        clubs = [c for c in seeded if float(c.get("miles", 999)) <= RADIUS_MILES]
        if only_clubs:
            clubs = [c for c in clubs if c["name"].lower() in only_clubs]
        clubs.sort(key=lambda c: float(c.get("miles", 999)))

        # Clubs proven to expose no availability grid on Playtomic are reported
        # explicitly instead of being navigated 7 times per run for nothing.
        no_feed = [c for c in clubs if c.get("feed", "playtomic") != "playtomic"]
        clubs = [c for c in clubs if c.get("feed", "playtomic") == "playtomic"]
        for c in no_feed:
            for day in days:
                report.append({
                    "club": c["name"], "date": day, "status": f"no-feed:{c['feed']}",
                    "slots": 0, "miles": c.get("miles"), "indoor": c.get("indoor"),
                    "note": c.get("feed_note", ""), "book_url": c.get("book_url", ""),
                })
            print(f"  {c['name']}: skipped - {c.get('feed_note', c['feed'])}", flush=True)

        print(f"discovered {len(clubs)} scrapable clubs within {RADIUS_MILES} mi of "
              f"{CENTER_COORD} ({len(no_feed)} without a Playtomic feed)", flush=True)

        for club in clubs:
            for day in days:
                try:
                    url = f"https://playtomic.com/clubs/{club['slug']}?date={day}"
                    goto_retry(page, url, attempts=3, label=f"{club['name']} {day}")
                    page.wait_for_timeout(int(os.getenv("SETTLE_MS", "1500")))
                    try:
                        page.wait_for_selector("[data-slot-id]", timeout=int(os.getenv("SELECTOR_TIMEOUT_MS", "5000")))
                    except Exception:  # noqa: BLE001 - club may simply be fully booked
                        pass
                    # Court headers carry the indoor/outdoor properties and render
                    # slightly after the slot cells; without this wait ~13% of
                    # club/day scans saw zero courts and fell back to the club flag.
                    try:
                        page.wait_for_selector(
                            '[style*="width:150px"]',
                            timeout=int(os.getenv("COURT_TIMEOUT_MS", "6000")))
                    except Exception:  # noqa: BLE001 - fall back to the club flag
                        pass
                    dom = page.evaluate(EXTRACT_JS)
                    if not club.get("_meta"):
                        try:
                            page.wait_for_load_state("networkidle", timeout=6000)
                        except Exception:  # noqa: BLE001
                            pass
                        try:
                            club["_meta"] = page.evaluate(META_JS)
                        except Exception:  # noqa: BLE001
                            club["_meta"] = {}
                    page_date = dom.get("page_date") or day
                    order = dom.get("resource_order") or []
                    courts = dom.get("courts") or []
                    orphans = dom.get("orphan_cells") or 0
                    if orphans:
                        print(f"   WARN {club['name']} {day}: {orphans} slot cells "
                              f"could not be tied to a court row", flush=True)
                    dom_slots = dom.get("slots", [])
                    _, inconsistent = audit_contiguity(dom_slots)
                    if inconsistent:
                        print(f"   WARN {club['name']} {day}: {inconsistent} long options "
                              f"are not backed by contiguous free half-hours", flush=True)
                    rows = []
                    dropped_short = 0
                    for s in dom_slots:
                        # keep only the durations we actually offer (60/90 by default):
                        # a 30 min gap is not a game, and 120 min is not what anyone books
                        if (s.get("duration") or 0) not in KEEP_DURATIONS:
                            dropped_short += 1
                            continue
                        f = court_facts(s, club["indoor"])
                        rows.append({
                            "date": page_date,
                            "club": club["name"],
                            "area": club["area"],
                            "drive": f"{club.get('miles')} mi from 33020",
                            "indoor": f["indoor"],
                            "indoor_source": f["indoor_source"],
                            "court_type": f["court_type"],
                            "wall": f["wall"],
                            "start": to_24h(s.get("time", "")),
                            "duration": s.get("duration"),
                            "price": None,
                            "court": f["court"],
                            "url": url,
                        })
                    # --- accordion source (authoritative court + real price) ---
                    prop_by_court = {(c.get("name") or ""): c
                                     for c in dom.get("courts", [])}
                    acc = page.evaluate(ACCORDION_JS)
                    acc_rows = []
                    acc_dropped = 0
                    for c in acc.get("courts", []):
                        f = court_facts(prop_by_court.get(c["court"], {"name": c["court"]}),
                                        club["indoor"])
                        f["court"] = c["court"]
                        if f["indoor_source"] == "club-default":
                            low = c["court"].lower()
                            if "indoor" in low or "outdoor" in low:
                                f["indoor"] = "indoor" in low
                                f["indoor_source"] = "court-name"
                        for o in c["options"]:
                            if o["duration"] not in KEEP_DURATIONS:
                                acc_dropped += 1
                                continue
                            acc_rows.append({
                                "date": page_date,
                                "club": club["name"],
                                "area": club["area"],
                                "drive": f"{club.get('miles')} mi from 33020",
                                "indoor": f["indoor"],
                                "indoor_source": f["indoor_source"],
                                "court_type": f["court_type"],
                                "wall": f["wall"],
                                "start": to_24h(o["start"]),
                                "duration": o["duration"],
                                "price": o["price"],
                                "court": f["court"],
                                "url": url,
                            })
                    source_mode = "cells"
                    if len(acc_rows) >= len(rows) and acc_rows:
                        rows = acc_rows
                        dropped_short = acc_dropped
                        source_mode = "accordion"
                    print(f"   {club['name']} {day}: source={source_mode} "
                          f"cells={len(dom_slots)} accordion_courts={len(acc.get('courts', []))} "
                          f"kept={len(rows)} priced={sum(1 for r in rows if r['price'])}",
                          flush=True)
                    items = order
                    status = "ok" if rows else ("no-slots" if page_date == day else "wrong-date")
                    all_rows.extend(rows)
                    report.append({
                        "club": club["name"], "date": day,
                        "status": status,
                        "slots": len(rows), "courts_seen": len(items),
                        "miles": club.get("miles"),
                        "indoor": club["indoor"],
                        "indoor_slots": sum(1 for r in rows if r["indoor"]),
                        "outdoor_slots": sum(1 for r in rows if not r["indoor"]),
                        "courts_found": len(courts),
                        "resources_seen": len(order),
                        "orphan_cells": orphans,
                        "dropped_other_durations": dropped_short,
                        "source_mode": source_mode,
                        "priced_slots": sum(1 for r in rows if r["price"]),
                        "accordion_courts": len(acc.get("courts", [])),
                        "inconsistent_long_options": inconsistent,
                        "court_detail": [f'{c["name"]} ({c["props"]})' for c in courts][:12],
                        "resources": len(items),
                        "url": url,
                        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    })
                    print(f"  {club['name']} {day}: {len(rows)} slots (http {status})", flush=True)
                except Exception as exc:  # noqa: BLE001
                    msg = f"{club['name']} {day}: {type(exc).__name__}"
                    errors.append(msg)
                    report.append({"club": club["name"], "date": day, "status": "error",
                                   "slots": 0, "error": type(exc).__name__, "miles": club.get("miles"),
                                   "url": f"https://playtomic.com/clubs/{club['slug']}",
                                   "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
                    print(f"  WARN {msg}", file=sys.stderr, flush=True)
        browser.close()

    return {
        "source": "playtomic-browser",
        "slots": all_rows,
        "report": report,
        "errors": errors,
        "days": days,
        "clubs": clubs,
        "center": CENTER_COORD,
        "radius_miles": RADIUS_MILES,
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "partial": bool(only_clubs or days_only or DAYS_ONLY),
    }


def main() -> int:
    payload = scrape()
    print(f"total: {len(payload['slots'])} slots, {len(payload['report'])} club/day scans, "
          f"{len(payload['errors'])} errors", flush=True)
    if not payload["report"]:
        print("nothing scanned at all - not calling the API", file=sys.stderr)
        return 1
    req = urllib.request.Request(API_INGEST, data=json.dumps(payload).encode(),
                                headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        print("ingest:", resp.status, resp.read().decode()[:300], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
