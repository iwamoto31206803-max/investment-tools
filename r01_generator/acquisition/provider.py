from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time as day_time, timedelta, timezone
from decimal import Decimal
import hashlib
from http.client import IncompleteRead, RemoteDisconnected
import json
import math
import re
import socket
import ssl
import time
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from ..acquisition_plan import AcquisitionPlan

TOKYO = ZoneInfo("Asia/Tokyo")
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class ProviderError(Exception):
    """Provider/transport/schema failure, separate from normalized data quality."""

    def __init__(self, reason: str, *, response_status=None, retry_count=0,
                 metadata=None, retrieved_at=None):
        super().__init__(reason)
        self.response_status = response_status
        self.retry_count = retry_count
        self.metadata = metadata or {}
        self.retrieved_at = retrieved_at


class TransientTransportError(Exception):
    pass


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


class HTTPTransport(Protocol):
    def get(self, url: str, *, timeout: float) -> HTTPResponse: ...


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_http(request: Request, *, timeout: float):
    # Retain default proxy/HTTPS handlers but never follow a different destination.
    return build_opener(NoRedirect()).open(request, timeout=timeout)


class UrllibTransport:
    """Explicitly constructed by --acquire; inherited proxy and TLS trust remain intact."""

    def get(self, url: str, *, timeout: float) -> HTTPResponse:
        request = Request(url, headers={"User-Agent": "R01-Development-Acquisition/0.2"})
        try:
            with open_http(request, timeout=timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ProviderError("response exceeds development size limit")
                return HTTPResponse(response.status, body, dict(response.headers))
        except HTTPError as exc:
            # Do not read or log error pages, credentials, or arbitrary server messages.
            return HTTPResponse(exc.code, b"", dict(exc.headers))
        except URLError as exc:
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise ProviderError("TLS certificate verification failed") from exc
            raise TransientTransportError("network request failed") from exc
        except (TimeoutError, socket.timeout, ConnectionError, IncompleteRead, RemoteDisconnected) as exc:
            raise TransientTransportError("network request failed") from exc


@dataclass(frozen=True)
class ProviderSettings:
    retry_limit: int = 3  # retries after the initial attempt
    backoff_initial_seconds: float = 1.0
    backoff_max_seconds: float = 8.0
    min_request_interval_seconds: float = 1.0
    timeout_seconds: float = 30.0

    def __post_init__(self):
        if type(self.retry_limit) is not int or not 0 <= self.retry_limit <= 10:
            raise ValueError("retry_limit must be an integer from 0 to 10")
        values = (self.backoff_initial_seconds, self.backoff_max_seconds,
                  self.min_request_interval_seconds, self.timeout_seconds)
        if any(isinstance(v, bool) or not isinstance(v, (int, float))
               or not math.isfinite(v) or v <= 0 for v in values):
            raise ValueError("provider timing values must be positive finite numbers")
        if self.backoff_initial_seconds > self.backoff_max_seconds:
            raise ValueError("initial backoff exceeds maximum")


def ticker_for(code: str, mappings: dict[str, str]) -> str:
    # Four-character JP codes may include the newer alphanumeric issue codes.
    if not isinstance(code, str) or not re.fullmatch(r"[0-9][0-9A-Z]{3}", code):
        raise ProviderError("invalid Japanese security code")
    ticker = mappings.get(code, f"{code}.T")
    if not isinstance(ticker, str) or not re.fullmatch(r"[0-9][0-9A-Z]{3}\.T", ticker):
        raise ProviderError("invalid Yahoo Tokyo ticker mapping")
    return ticker


def request_timestamps(start: date, end: date) -> tuple[int, int]:
    if type(start) is not date or type(end) is not date or start > end:
        raise ProviderError("explicit ordered request dates required")
    try:
        first = datetime.combine(start, day_time.min, TOKYO)
        exclusive_end = datetime.combine(end + timedelta(days=1), day_time.min, TOKYO)
    except OverflowError as exc:
        raise ProviderError("request end date cannot be represented") from exc
    return int(first.timestamp()), int(exclusive_end.timestamp())


@dataclass(frozen=True)
class ProviderPayload:
    chart: dict
    ticker: str
    retrieved_at: datetime
    response_status: int
    retry_count: int
    metadata: dict


class AcquisitionProvider(Protocol):
    def fetch(self, plan: AcquisitionPlan) -> ProviderPayload: ...


class FallbackProvider(AcquisitionProvider, Protocol):
    """Future approved adapter contract; Phase 2A configures no fallback."""


class YahooChartProvider:
    def __init__(self, transport: HTTPTransport, *, settings: ProviderSettings | None = None,
                 mappings: dict[str, str] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.transport = transport
        self.settings = settings or ProviderSettings()
        self.mappings = dict(mappings or {})
        for code in self.mappings:
            ticker_for(code, self.mappings)
        self.sleep, self.clock, self.now = sleep, clock, now
        self._last_request: float | None = None

    def fetch(self, plan: AcquisitionPlan) -> ProviderPayload:
        # Defence in depth: provider cannot supply or expand a missing start.
        from ..acquisition_plan import AcquisitionMode, PlanStatus
        if (plan.plan_status is not PlanStatus.READY or
                not isinstance(plan.acquisition_mode, AcquisitionMode) or
                plan.acquisition_mode not in {AcquisitionMode.OVERLAP,
                    AcquisitionMode.BACKFILL, AcquisitionMode.FULL_REFRESH}):
            raise ProviderError("provider requires a READY acquisition plan")
        if (plan.acquisition_mode is AcquisitionMode.FULL_REFRESH and
                not (plan.full_refresh_reason and plan.full_refresh_reason.strip())):
            raise ProviderError("FULL_REFRESH requires an explicit reason")
        ticker = ticker_for(plan.security_code, self.mappings)
        first, last = request_timestamps(plan.request_start_date, plan.request_end_date)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?" + urlencode({
            "period1": first, "period2": last, "interval": "1d", "includeAdjustedClose": "true"})
        for retry in range(self.settings.retry_limit + 1):
            if self._last_request is not None:
                wait = self.settings.min_request_interval_seconds - (self.clock() - self._last_request)
                if wait > 0:
                    self.sleep(wait)
            self._last_request = self.clock()
            response = None
            metadata = {"period1": first, "period2_exclusive": last, "interval": "1d"}
            retrieved_at = None
            try:
                response = self.transport.get(url, timeout=self.settings.timeout_seconds)
                retrieved_at = self.now()
                if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
                    raise ProviderError("retrieval timestamp must be timezone-aware")
                metadata.update({"response_sha256": hashlib.sha256(response.body).hexdigest(),
                                 "response_bytes": len(response.body)})
                if response.status == 429 or 500 <= response.status < 600:
                    raise TransientTransportError("transient HTTP failure")
                if response.status != 200:
                    raise ProviderError(f"permanent HTTP status {response.status}")
                if len(response.body) > MAX_RESPONSE_BYTES:
                    raise ProviderError("response exceeds development size limit")
                try:
                    payload = json.loads(response.body, parse_float=Decimal)
                    chart = payload["chart"]
                    if chart.get("error") is not None:
                        raise ProviderError("Yahoo chart returned an error; fallback unavailable")
                    results = chart["result"]
                    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
                        raise ProviderError("malformed Yahoo result schema")
                    result = results[0]
                    meta = result["meta"]
                    if (not isinstance(meta, dict) or meta.get("symbol") != ticker or
                            meta.get("exchangeTimezoneName") != "Asia/Tokyo"):
                        raise ProviderError("Yahoo symbol or exchange timezone mismatch")
                    # Preserve raw response metadata, not the price arrays or arbitrary headers.
                    if len(json.dumps(meta, default=str)) > 8192:
                        raise ProviderError("provider metadata exceeds development size limit")
                    metadata["yahoo_meta"] = meta
                    return ProviderPayload(result, ticker, retrieved_at, response.status, retry, metadata)
                except (ValueError, KeyError, TypeError, AttributeError) as exc:
                    raise ProviderError("malformed Yahoo payload") from exc
            except ProviderError as exc:
                raise ProviderError(str(exc), response_status=response.status if response else None,
                                    retry_count=retry, metadata=metadata,
                                    retrieved_at=retrieved_at) from exc
            except (TransientTransportError, TimeoutError, ConnectionError, IncompleteRead) as exc:
                if retry == self.settings.retry_limit:
                    raise ProviderError("transient retries exhausted; fallback unavailable",
                        response_status=response.status if response else None,
                        retry_count=retry, metadata=metadata, retrieved_at=retrieved_at) from exc
                self.sleep(min(self.settings.backoff_max_seconds,
                               self.settings.backoff_initial_seconds * 2 ** retry))
        raise AssertionError("unreachable")
