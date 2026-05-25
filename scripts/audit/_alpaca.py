"""Minimal Alpaca API helper for audit scripts. Read-only."""
from __future__ import annotations
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]


def _load_env_paper():
    env_file = ROOT / ".env.paper"
    creds = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if "#" in v and not v.startswith('"'):
            v = v.split("#", 1)[0].strip()
        creds[k.strip()] = v
    return creds


_ENV = _load_env_paper()

API_KEY = _ENV.get("ALPACA_API_KEY") or os.environ.get("ALPACA_API_KEY")
SECRET_KEY = _ENV.get("ALPACA_SECRET_KEY") or os.environ.get("ALPACA_SECRET_KEY")
BASE = _ENV.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2").rstrip("/")
DATA_BASE = "https://data.alpaca.markets/v2"

HEADERS = {
    "APCA-API-KEY-ID": API_KEY or "",
    "APCA-API-SECRET-KEY": SECRET_KEY or "",
}


def _get(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def get(path: str, params: Optional[dict] = None, data_api: bool = False):
    base = DATA_BASE if data_api else BASE
    if not path.startswith("/"):
        path = "/" + path
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        return _get(url)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code} on {url}: {body}") from None


def iter_orders(status: str = "all", after: Optional[str] = None, until: Optional[str] = None,
                direction: str = "desc", nested: bool = True, limit: int = 500, max_pages: int = 50):
    """Page through /v2/orders. Alpaca caps at 500 per page."""
    params = {"status": status, "limit": limit, "direction": direction, "nested": str(nested).lower()}
    if after:
        params["after"] = after
    if until:
        params["until"] = until
    seen = set()
    for page in range(max_pages):
        batch = get("/orders", params)
        if not batch:
            return
        new_batch = []
        for o in batch:
            oid = o.get("id")
            if oid in seen:
                continue
            seen.add(oid)
            new_batch.append(o)
        for o in new_batch:
            yield o
        if len(batch) < limit:
            return
        # Page by submitted_at; direction=desc → move 'until' to the oldest
        last = new_batch[-1] if new_batch else batch[-1]
        ts = last.get("submitted_at") or last.get("created_at")
        if not ts:
            return
        if direction == "desc":
            params["until"] = ts
        else:
            params["after"] = ts
        time.sleep(0.15)


def iter_activities(activity_types: str = "FILL", after: Optional[str] = None, max_pages: int = 50):
    params = {"activity_types": activity_types, "page_size": 100, "direction": "desc"}
    if after:
        params["after"] = after
    for page in range(max_pages):
        batch = get("/account/activities", params)
        if not batch:
            return
        for a in batch:
            yield a
        if len(batch) < 100:
            return
        last = batch[-1]
        params["page_token"] = last.get("id")
        time.sleep(0.15)


if __name__ == "__main__":
    acct = get("/account")
    print(f"status={acct.get('status')} equity={acct.get('equity')} cash={acct.get('cash')}")
