#!/usr/bin/env python3
"""Publish the generated drive matrix and the spend ledger into a ConfigMap.

The CronJob pod is ephemeral, which would defeat two things: the API needs the fresh
matrix, and the budget guards need the ledger to survive between runs. Both live in one
ConfigMap, mounted read-only by the API and by the next CronJob run.

Uses the pod's own ServiceAccount against the in-cluster API, so no kubectl binary and no
extra image are required. Only get/patch on one named ConfigMap is granted.
"""
import json
import os
import pathlib
import sys

import httpx

SA = pathlib.Path("/var/run/secrets/kubernetes.io/serviceaccount")
CM_NAME = os.getenv("DRIVE_CONFIGMAP", "padel-drive-state")
OUT = pathlib.Path(os.getenv("DRIVE_OUT", "/state/drive.json"))
LEDGER = pathlib.Path(os.getenv("DRIVE_LEDGER", "/state/drive-usage.json"))


def publish(data):
    ns = (SA / "namespace").read_text().strip()
    token = (SA / "token").read_text().strip()
    url = f"https://kubernetes.default.svc/api/v1/namespaces/{ns}/configmaps/{CM_NAME}"
    r = httpx.patch(
        url,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/merge-patch+json"},
        json={"data": data},
        verify=str(SA / "ca.crt"),
        timeout=30.0,
    )
    if r.status_code >= 400:
        sys.exit(f"patch failed HTTP {r.status_code}: {r.text[:200]}")


def main():
    # --ledger-only is the abort path: an interrupted run still has to report what it spent,
    # otherwise the daily and monthly guards lose count of calls that really happened.
    ledger_only = "--ledger-only" in sys.argv
    if ledger_only:
        if not LEDGER.exists():
            print("no ledger to publish")
            return
        publish({"drive-usage.json": LEDGER.read_text()})
        print("published ledger only")
        return
    if not OUT.exists():
        sys.exit(f"nothing to publish: {OUT} is missing")
    # Refuse to publish a matrix that is not shaped like a matrix.
    payload = json.loads(OUT.read_text())
    if not payload.get("matrix") or len(payload["matrix"]) < 1:
        sys.exit("refusing to publish an empty matrix")
    if "pk." in OUT.read_text() or "sk." in OUT.read_text():
        sys.exit("refusing to publish: looks like a token leaked into the data")

    data = {"drive.json": OUT.read_text()}
    if LEDGER.exists():
        data["drive-usage.json"] = LEDGER.read_text()
    publish(data)
    print(f"published {len(payload['matrix'])} origins to configmap/{CM_NAME} "
          f"(provider={payload.get('provider')}, traffic={payload.get('traffic_aware')})")


if __name__ == "__main__":
    main()
