"""Shared HTTP plumbing: backoff, raw-body hashing, cross-process file lock."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx

from gexlab import config


class SourceError(RuntimeError):
    """A vendor request failed after retries, or returned something unusable."""

    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


@dataclass
class VenuePart:
    """What one exchange contributed to a crypto map: its contracts, its own index price and its provenance.
    The aggregator (sources/crypto.py) merges parts into one Chain."""
    venue: str                                  # deribit | okx | bybit | binance
    contracts: list
    spot: object                                # models.Spot: this venue's index
    valuation_ts: datetime | None               # the venue's own server clock for the snapshot
    source_ts: datetime | None = None           # newest quote time in the payload
    raw_hash: str | None = None
    requests: int = 0
    warnings: list[str] = field(default_factory=list)


class NoOptions(SourceError):
    """The venue answered, and it lists no options for this coin. Not a failure: it is the venue's answer."""

    def __init__(self, msg: str):
        super().__init__(msg, status=404)


class Hasher:
    """sha256 over the raw response bodies in fetch order (stored as response_hash)."""

    def __init__(self):
        self._h = hashlib.sha256()
        self.count = 0

    def add(self, body: bytes) -> None:
        self._h.update(len(body).to_bytes(8, "big"))
        self._h.update(body)
        self.count += 1

    def hexdigest(self) -> str:
        return self._h.hexdigest()


def get_with_backoff(client: httpx.Client, url: str, *, params: dict | None = None,
                     headers: dict | None = None,
                     sleep: Callable[[float], None] = time.sleep) -> httpx.Response:
    """GET with exponential backoff on 429 / 5xx / network errors.

    4xx other than 429 are returned to the caller unretried (403 means "not
    entitled", which feed detection needs to see).
    """
    last_exc: Exception | None = None
    for attempt in range(config.HTTP_MAX_RETRIES + 1):
        try:
            r = client.get(url, params=params, headers=headers)
        except httpx.HTTPError as e:
            last_exc = e
            r = None
        if r is not None and r.status_code < 500 and r.status_code != 429:
            return r
        if attempt == config.HTTP_MAX_RETRIES:
            break
        wait = config.HTTP_BACKOFF_BASE_S * (2 ** attempt)
        if r is not None:
            ra = r.headers.get("Retry-After")
            if ra and ra.replace(".", "", 1).isdigit():
                wait = max(wait, float(ra))
        sleep(wait)
    if r is not None:
        raise SourceError(f"GET {url} failed with HTTP {r.status_code}", status=r.status_code)
    raise SourceError(f"GET {url} failed: {type(last_exc).__name__}")


# ---------------------------------------------------------------- cross-process file lock

@contextmanager
def file_lock(path: Path | str, timeout_s: float = 60.0, poll_s: float = 0.05):
    """Exclusive lock on `path` shared by every process on this machine (msvcrt on Windows,
    fcntl elsewhere). Raises SourceError when the lock is not acquired within timeout_s."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise SourceError(f"timed out waiting for lock {path.name}")
                time.sleep(poll_s)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


# Windows: os.replace fails with WinError 5 (access denied) or 32 (sharing violation) while another
# process briefly holds the destination open without FILE_SHARE_DELETE, typically Defender or the
# search indexer scanning a file that was just written. It clears within milliseconds, so retry.
REPLACE_RETRY_S = (0.01, 0.02, 0.05, 0.1, 0.2, 0.4, 0.8)


def replace_with_retry(src: Path | str, dest: Path | str, waits: tuple[float, ...] = REPLACE_RETRY_S) -> None:
    for wait in waits:
        try:
            os.replace(src, dest)
            return
        except PermissionError:
            if os.name != "nt":
                raise
            time.sleep(wait)
    os.replace(src, dest)          # last attempt raises the real error


def atomic_write_bytes(dest: Path | str, data: bytes) -> None:
    """Write to a UNIQUE temp file in the same folder, then atomically replace dest."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=dest.name + ".", suffix=".part", dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        replace_with_retry(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
