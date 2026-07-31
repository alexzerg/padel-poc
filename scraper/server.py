"""Warm scraper service.

A Job per on-demand check cost 40-60 s of pod scheduling and image pull for 5 s
of actual work. This keeps a long-lived pod instead, so the UI "Check" button
gets an answer synchronously in a few seconds. The periodic full scan still runs
as the CronJob; this service handles targeted re-checks.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

import scrape

LOG = logging.getLogger("scraper-svc")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

app = FastAPI(title="padel-scraper", version=os.getenv("APP_VERSION", "0.1.0"))
_lock = threading.Lock()  # one browser at a time keeps memory predictable


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": app.version, "busy": _lock.locked()}


@app.post("/scrape")
def do_scrape(
    club: str | None = Query(default=None),
    day: str | None = Query(default=None, alias="date"),
    ingest: bool = True,
) -> JSONResponse:
    if _lock.locked():
        return JSONResponse({"error": "busy, retry"}, status_code=429)
    with _lock:
        payload = scrape.scrape(
            clubs_only=[club] if club else None,
            days_only=[day] if day else None,
        )
    slots = len(payload["slots"])
    if ingest and payload["report"]:
        try:
            req = urllib.request.Request(
                scrape.API_INGEST,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("ingest failed: %s", exc)
    return JSONResponse({
        "club": club,
        "date": day,
        "slots": slots,
        "verdict": "confirmed" if slots else "cancelled",
        "report": payload["report"],
        "scanned_at": payload["scanned_at"],
    })
