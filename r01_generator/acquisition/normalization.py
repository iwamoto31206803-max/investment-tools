from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .models import Observation
from .provider import ProviderError, ProviderPayload, TOKYO


class DataQualityError(Exception):
    pass


def numeric(value) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise DataQualityError("non-numeric OHLCV value")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise DataQualityError("invalid numeric value") from exc
    if not number.is_finite():
        raise DataQualityError("non-finite OHLCV value")
    return number


def normalize(payload: ProviderPayload, security_code: str) -> tuple[list[Observation], list[str]]:
    """Preserve provider ordering and missing cells; validation decides usability."""
    chart = payload.chart
    timestamps = chart.get("timestamp", [])
    if timestamps is None:
        timestamps = []
    if not isinstance(timestamps, list):
        raise ProviderError("timestamp array required")
    try:
        indicators = chart["indicators"]
        quotes = indicators["quote"]
        if not isinstance(quotes, list) or len(quotes) != 1 or not isinstance(quotes[0], dict):
            raise ProviderError("one quote object required")
        quote = quotes[0]
        columns = {}
        for key in ("open", "high", "low", "close"):
            columns[key] = quote.get(key, []) if not timestamps else quote[key]
        columns["volume"] = quote.get("volume", [None] * len(timestamps))
        adjusted = indicators.get("adjclose")
        if adjusted is None or adjusted == []:
            columns["adjclose"] = [None] * len(timestamps)
        else:
            if not isinstance(adjusted, list) or len(adjusted) != 1:
                raise ProviderError("one adjusted-close object required")
            columns["adjclose"] = adjusted[0]["adjclose"]
        if any(not isinstance(values, list) or len(values) != len(timestamps)
               for values in columns.values()):
            raise ProviderError("Yahoo column lengths differ from timestamps")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ProviderError("malformed Yahoo indicators") from exc
    observations, warnings = [], []
    for index, timestamp in enumerate(timestamps):
        if isinstance(timestamp, bool) or not isinstance(timestamp, int):
            raise ProviderError("daily timestamp must be integer epoch seconds")
        try:
            observed = datetime.fromtimestamp(timestamp, timezone.utc).astimezone(TOKYO).date()
        except (ValueError, OverflowError, OSError) as exc:
            raise ProviderError("invalid Yahoo timestamp") from exc
        values = [numeric(columns[key][index]) for key in
                  ("open", "high", "low", "close", "adjclose", "volume")]
        observations.append(Observation(security_code, observed, *values,
                                        "Yahoo Chart API", payload.retrieved_at))
    if any(row.ADJUSTED_CLOSE is None for row in observations):
        warnings.append("adjusted close unavailable for one or more observations")
    if any(row.VOLUME is None for row in observations):
        warnings.append("volume unavailable for one or more observations")
    return observations, warnings
