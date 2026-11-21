"""Crash-safe JSON writes: write to a temp file then os.replace() over the
target, so a crash mid-write never leaves a truncated/corrupt file."""
import json
import os
import tempfile
import time

REPLACE_RETRIES = 8
REPLACE_WAIT_SECONDS = 0.25


def write(path, data):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        for attempt in range(REPLACE_RETRIES):  # a reader (dedup_server) briefly locks the target on Windows, WinError 32 on 2026-11-21 killed a scoring run twice
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == REPLACE_RETRIES - 1:
                    raise
                time.sleep(REPLACE_WAIT_SECONDS * (attempt + 1))
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
