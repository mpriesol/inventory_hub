"""Shared admission for every configured Hub→Upgates HTTP request.

A separate autocommit PostgreSQL connection holds one session advisory lock,
never a business transaction/row lock, over network I/O. One request per HTTPS
origin leaves room below Upgates' group-wide limit of three. Other applications
are not controlled here; their usage is observed in headers. No HTTP retries.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import math
from threading import Lock, RLock
from time import monotonic, sleep
from urllib.parse import urlsplit

import psycopg2
import requests

from inventory_hub.settings import settings

LOCK_WAIT_SECONDS = 1.0
UNKNOWN_RETRY_SECONDS = 60
MAX_RETRY_SECONDS = 604800


class AdmissionDenied(Exception):
    def __init__(self, retry_after=UNKNOWN_RETRY_SECONDS):
        self.retry_after = max(1, min(MAX_RETRY_SECONDS, int(retry_after)))
        super().__init__("Upgates API request deferred before sending")


def now():
    return datetime.now(timezone.utc)


def target_key(url):
    """Pool all API logins/paths on an origin; never retain URL or credentials."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValueError
        origin = f"https://{parsed.hostname.lower()}:{parsed.port or 443}"
        return hashlib.sha256(origin.encode("ascii")).hexdigest()
    except (TypeError, ValueError, UnicodeError):
        raise AdmissionDenied() from None


def _number(value):
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 10:
        result = int(value)
        if result <= 1_000_000_000:
            return result
    return None


def _retry_after(value, at):
    if not isinstance(value, str) or len(value) > 100:
        return UNKNOWN_RETRY_SECONDS
    try:
        if value.strip().isascii() and value.strip().isdigit():
            seconds = int(value.strip())
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                raise ValueError
            seconds = math.ceil((date - at).total_seconds())
        return max(1, min(MAX_RETRY_SECONDS, seconds))
    except (TypeError, ValueError, OverflowError):
        return UNKNOWN_RETRY_SECONDS


@dataclass
class BudgetState:
    hour_remaining: int | None = None
    day_remaining: int | None = None
    observed_hour: datetime | None = None
    blocked_until: datetime | None = None
    requests_started: int = 0

    def reserve(self, at):
        if self.blocked_until is not None and self.blocked_until > at:
            raise AdmissionDenied(math.ceil((self.blocked_until - at).total_seconds()))
        hour = at.replace(minute=0, second=0, microsecond=0)
        if self.observed_hour != hour:
            # A new hour permits one probe, not an invented replenished quota.
            self.hour_remaining = None
            self.observed_hour = hour
        if self.hour_remaining is None or self.day_remaining is None:
            self.blocked_until = at + timedelta(seconds=UNKNOWN_RETRY_SECONDS)
        elif self.hour_remaining > 0:
            self.hour_remaining -= 1
        elif self.day_remaining > 0:
            self.day_remaining -= 1
        else:
            self.blocked_until = hour + timedelta(hours=1, seconds=1)
            raise AdmissionDenied(math.ceil((self.blocked_until - at).total_seconds()))
        self.requests_started += 1

    def observe(self, headers, status, at):
        hour = _number(headers.get("X-Rate-Limit-Hour-Remaining"))
        day = _number(headers.get("X-Rate-Limit-Day-Remaining"))
        hour_limit = _number(headers.get("X-Rate-Limit-Hour"))
        day_limit = _number(headers.get("X-Rate-Limit-Day"))
        if hour_limit is not None and hour is not None and hour > hour_limit:
            hour = None
        if day_limit is not None and day is not None and day > day_limit:
            day = None
        if hour is not None and day is not None:
            self.hour_remaining, self.day_remaining = hour, day
            self.observed_hour = at.replace(minute=0, second=0, microsecond=0)
            self.blocked_until = None
        if status == 429:
            self.blocked_until = at + timedelta(seconds=_retry_after(headers.get("Retry-After"), at))


def _connect():
    # kwargs avoid URI interpolation and accidental credential-bearing errors.
    return psycopg2.connect(host=settings.DB_HOST, port=settings.DB_PORT, dbname=settings.DB_NAME,
                            user=settings.DB_USER, password=settings.DB_PASSWORD,
                            connect_timeout=2, options="-c statement_timeout=1500")


class DatabasePermit:
    def __init__(self, key):
        self.connection = None
        self.key = key
        self.lock_id = int.from_bytes(bytes.fromhex(key)[:8], "big", signed=True)
        try:
            self.connection = _connect()
            self.connection.autocommit = True
            deadline = monotonic() + LOCK_WAIT_SECONDS
            with self.connection.cursor() as cursor:
                while True:
                    cursor.execute("SELECT pg_try_advisory_lock(%s)", (self.lock_id,))
                    if cursor.fetchone()[0]:
                        break
                    if monotonic() >= deadline:
                        raise AdmissionDenied(2)
                    sleep(0.05)
                cursor.execute("INSERT INTO upgates_api_budget(target_key) VALUES (%s) ON CONFLICT DO NOTHING", (key,))
                cursor.execute("SELECT hour_remaining, day_remaining, observed_hour, blocked_until, requests_started "
                               "FROM upgates_api_budget WHERE target_key = %s", (key,))
                self.state = BudgetState(*cursor.fetchone())
            self.state.reserve(now())
            self._save()
        except AdmissionDenied:
            self.close()
            raise
        except Exception:
            self.close()
            raise AdmissionDenied() from None
        except BaseException:
            self.close()
            raise

    def _save(self):
        with self.connection.cursor() as cursor:
            cursor.execute("UPDATE upgates_api_budget SET hour_remaining=%s, day_remaining=%s, observed_hour=%s, "
                           "blocked_until=%s, requests_started=%s, updated_at=now() WHERE target_key=%s",
                           (self.state.hour_remaining, self.state.day_remaining, self.state.observed_hour,
                            self.state.blocked_until, self.state.requests_started, self.key))

    def observe(self, headers, status):
        self.state.observe(headers, status, now())
        try:
            self._save()
        except Exception:
            # The request already happened. Accounting failure must never
            # turn a received response into an automatic HTTP retry.
            pass

    def close(self):
        connection, self.connection = self.connection, None
        if connection is not None:
            try:
                connection.close()  # releases the session lock after SQL errors too
            except Exception:
                pass


_local_registry_lock = Lock()
_local_registry = {}


class LocalPermit:
    """Explicit legacy USE_POSTGRES=false mode: one process, not distributed."""
    def __init__(self, key):
        with _local_registry_lock:
            lock, state = _local_registry.setdefault(key, (Lock(), BudgetState()))
        if not lock.acquire(timeout=LOCK_WAIT_SECONDS):
            raise AdmissionDenied(2)
        self.lock, self.state = lock, state
        try:
            state.reserve(now())
        except BaseException:
            self.close()
            raise

    def observe(self, headers, status):
        self.state.observe(headers, status, now())

    def close(self):
        lock, self.lock = self.lock, None
        if lock is not None:
            lock.release()


def acquire(key):
    # No in-memory fallback when production PostgreSQL is unavailable.
    return DatabasePermit(key) if settings.USE_POSTGRES else LocalPermit(key)


def _deferred(delay):
    response = requests.Response()
    response.status_code = 429
    response.headers["Retry-After"] = str(delay)
    response.headers["Content-Type"] = "application/json"
    response._content = b'{"messages":[{"level":"error","message":"Upgates API request deferred before sending"}]}'
    response._content_consumed = True
    response.hub_request_sent = False
    return response


class BudgetSession(requests.Session):
    def __init__(self, base_url):
        super().__init__()
        self.budget_key = target_key(base_url)
        self._responses = {}
        self._response_lock = RLock()

    def request(self, method, url, **kwargs):
        try:
            parsed = urlsplit(url)
            if target_key(f"{parsed.scheme}://{parsed.netloc}") != self.budget_key:
                raise AdmissionDenied()
            permit = acquire(self.budget_key)
        except (TypeError, ValueError):
            return _deferred(UNKNOWN_RETRY_SECONDS)
        except AdmissionDenied as error:
            return _deferred(error.retry_after)
        # Redirects may not escape the origin pool or forward API credentials.
        kwargs["allow_redirects"] = False
        try:
            response = super().request(method, url, **kwargs)
            response.hub_request_sent = True
            permit.observe(response.headers, response.status_code)
            if kwargs.get("stream"):
                original_close = response.close
                token = id(response)

                def close():
                    try:
                        original_close()
                    finally:
                        with self._response_lock:
                            held = self._responses.pop(token, None)
                        if held is not None:
                            held[1].close()

                with self._response_lock:
                    self._responses[token] = (close, permit)
                response.close = close
                permit = None
            return response
        finally:
            if permit is not None:
                permit.close()

    def close(self):
        with self._response_lock:
            pending = list(self._responses.values())
        for close, _permit in pending:
            try:
                close()
            except Exception:
                pass
        super().close()
