"""Every model call goes through this cache, keyed by the exact request body.

A rerun builds the same requests, finds the same files, and calls nothing, so
it costs nothing and changes nothing. The cache is committed with the repo,
which lets anyone recompute every number without a key."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import tempfile


def request_key(request: dict) -> str:
    blob = json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class Cache:
    def __init__(self, root: str):
        self.root = root

    def path(self, namespace: str, request: dict) -> str:
        key = request_key(request)
        return os.path.join(self.root, namespace, key[:2], key + ".json")

    def get(self, namespace: str, request: dict) -> dict | None:
        p = self.path(namespace, request)
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f)["response"]

    def has(self, namespace: str, request: dict) -> bool:
        return os.path.exists(self.path(namespace, request))

    def put(self, namespace: str, request: dict, response: dict) -> None:
        p = self.path(namespace, request)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        record = {
            "cached_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "request": request,
            "response": response,
        }
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
