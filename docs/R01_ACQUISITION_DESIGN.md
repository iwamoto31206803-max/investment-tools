# R01 StockFacts Generator — Phase 2A: Acquisition Engine

**Development only. Production Not Yet Approved.** Phase 1's reader, planner,
sanity gate, and original tests (including the 665-security/96-observation
regression) are preserved. The generator's compatibility version stays
`0.1.0-dev`; evidence separately identifies acquisition engine `0.2.0-dev`.
Neither version is an Approved Release.

## Architecture

The CLI separates these stages:

1. Read the input ZIP/XML and retain its SHA-256 and per-security date indexes.
2. Load an explicit development contract; select its securities (including new
   codes absent from `stock_daily`), call the existing planner, and materialize
   previously unbounded BACKFILL/FULL_REFRESH starts from approved contract dates.
3. Preflight the entire batch against existing history, plan status, exact
   windows, expected counts, override evidence, and run-level evidence. Any invalid
   plan aborts the batch with zero provider calls. No provider can plan or enlarge
   a request window.
4. Fetch daily Yahoo Chart responses with an injected `HTTPTransport`; normalize
   them separately to typed `Observation` objects; validate before exposing rows.
5. Write a new development workspace containing normalized JSONL and run JSON.
   Rehash the read-only input, including on acquisition failure.

`acquisition/contract.py` owns development input/planning; `provider.py` owns
transport, exact request encoding, metadata, and retries; `normalization.py` owns
schema parsing and numeric conversion; `engine.py` owns admission, sequential
execution, quality checks and completeness states; `models.py` owns typed results,
serialization and hashes. `FallbackProvider` defines a future approved provider
interface. Phase 2A instantiates no fallback and fails closed on provider failure.

## Explicit development input contract

Phase 1 does not read a universe sufficient to add new securities, and its
BACKFILL/FULL_REFRESH plans have no start date. Those omissions must never be
resolved through provider guesses, implicit epoch starts, or automatic window
expansion. `--acquire` requires a JSON contract with schema version 1, the actual
input SHA-256, the exact analysis date, a nonempty development approval/reference
`decision_source`, and an explicit selected-security list:

```json
{
  "schema_version": 1,
  "input_sha256": "<actual 64 lowercase hex SHA-256 of R01.xlsx>",
  "analysis_date": "2026-09-24",
  "decision_source": "development-review-2A-001",
  "securities": [
    {"security_code": "7203"},
    {"security_code": "130A", "request_start_date": "2025-01-01"},
    {"security_code": "6758", "request_start_date": "2020-01-01",
     "full_refresh_reason": "approved development source correction"}
  ]
}
```

This is an explicit **selection**, not a claim that it is the complete weekly
universe. Omitted securities are not acquired. A `request_start_date` is required
for BACKFILL and FULL_REFRESH and prohibited for OVERLAP/NO_DATA_CHANGE; overlap
starts always come from the existing planner. The request end is the analysis
date for all securities. FULL_REFRESH requires a nonempty reason. BACKFILL for
short existing history cannot start after the latest existing observation.

`yahoo_ticker` is an optional per-security mapping. Default is `<code>.T`.
Validated four-character Japanese codes start with a digit and may contain
uppercase letters (e.g. `130A`). This adapter accepts only four-character `.T`
tickers and verifies the returned symbol and exchange timezone. Unsupported
markets fail closed. The explicit mapping is the reviewable source of ticker
identity; no equivalence to a formal security master is claimed.

An optional `run_decision` contains the Phase 1 boolean fields
`no_new_price_facts` (required when present), `universe_changed`,
`corporate_action_refresh_required`, and `other_fact_change`. The planner rejects
contradictions and new/insufficient-history selections. Engine admission requires
that evidence again; NO_DATA_CHANGE produces zero provider calls and zero rows.
The CLI contract does not offer an overlap override. Python callers can pass
existing structured `OverrideEvidence`; the engine verifies its flag, actual
window, nonempty decision fields, ordered original window and aware timestamp.

Unknown fields, duplicate JSON keys/codes, mismatched input/date, empty reasons,
and incomplete window approvals fail closed. The contract is limited to 1 MiB
and 10,000 explicitly selected securities. Its canonical hash participates in
acquisition configuration evidence. This is a development declaration, not an
authorization system or a change to Standards, TA/TB, Manifest or Current Baseline.
Do not include credentials or secret values in the contract.

## Request boundaries and transport

An approved start/end pair is inclusive in exchange dates. Yahoo `period1` is
midnight at start in Asia/Tokyo; `period2` is midnight **after** end in Asia/Tokyo.
Both become UTC epoch seconds. No `range` shorthand, provider-side history-length
heuristic, or retry-time window expansion is permitted. The request fixes
`interval=1d` and `includeAdjustedClose=true`. Returned timestamps are converted
from UTC epoch to Tokyo dates before range validation.

`YahooChartProvider` requires a transport supplied by its caller. Only explicit
CLI `--acquire` constructs `UrllibTransport`; omitted mode defaults to offline
planning and `--dry-run` is mutually exclusive with acquisition. The urllib
transport retains inherited proxy settings and TLS verification, rejects redirects, uses a 30-second
timeout, and caps response bodies at 16 MiB. Exchange/symbol mismatches and malformed
chart/indicator structures are provider errors. A valid empty response has one
result with matching metadata, an absent/empty timestamp array and matching empty
quote columns (including Yahoo's empty quote object).

No live Yahoo calls are performed by tests. A root autouse fixture blocks socket
connections even for legacy tests. Transport error handling is tested by injected
exceptions; CLI acquisition is tested with fake transports.

## Retry and execution policy

Execution is sequential (`--execution sequential`); there is no implicit thread
pool. The same provider instance rate-limits all security requests and retry
attempts to at least one second between starts. Sleep, monotonic clock and retrieval
clock are injectable. `ProviderSettings` validates timing values and bounds
`retry_limit` to 0..10. The CLI uses the existing `provider_retry_count` config,
default 3, as **additional retries** (maximum four attempts).

Transient network failures (including interrupted reads), HTTP 429, and HTTP
5xx retry with delays `min(8, 1 * 2**retry_index)` seconds by default. Minimum
request spacing is also enforced. Permanent HTTP failures, invalid mappings,
TLS certificate verification failures, schema errors, and data-quality failures
do not retry. No retry splits a security's window. Segmented retry is unimplemented;
there is no evidence requiring that additional complexity in Phase 2A.

Provider error messages do not include arbitrary response descriptions, HTTP
error pages, authentication headers, or underlying exception strings. No cookies,
tokens, credential requirements, or alternative providers are introduced.

## Normalized schema and quality checks

Each typed observation has:

| Field | Type / semantics |
| --- | --- |
| SECURITY_CODE | Explicit selected Japanese code; retained independently of mapped ticker |
| OBSERVATION_DATE | Tokyo exchange date |
| OPEN, HIGH, LOW, CLOSE | Unadjusted `Decimal` or missing (`None`) |
| ADJUSTED_CLOSE | Provider adjusted close as `Decimal`, independently missing |
| VOLUME | Non-negative integral `Decimal`, independently missing |
| SOURCE | `Yahoo Chart API` |
| RETRIEVED_AT | Aware UTC retrieval timestamp (default clock) |

JSONL represents Decimal values as exact decimal **strings** and missing values
as JSON `null`. Decimal JSON parsing avoids converting provider decimal tokens
through binary floats. Each record adds `LINEAGE` with plan hash, acquisition mode,
request bounds, provider status, and ticker. Missing adjusted close is never filled
from Close; missing volume is never replaced by zero. Missing optional adjusted
close/volume raises warnings while retaining otherwise valid observations.

Validation rejects duplicate keys, unordered dates (rather than silently sorting
or deduplicating), security mismatch, nonnumeric/nonfinite/negative values,
fractional volume, inconsistent OHLC bounds wherever comparable fields exist,
and any date outside the approved request. Invalid batches for a security expose
no normalized rows. Partially missing required OHLC retains explicit nulls and
marks the security PARTIAL. Zero volume is valid; weekend/holiday gaps are retained
as gaps, with no fabricated price facts or dates.

OVERLAP compares returned dates with the exact **existing** history dates within
the approved window, not all intervening calendar days or guessed future trading
dates. New observations after the latest R01 date are allowed through the request
end. Missing known overlap dates is PARTIAL. FULL_REFRESH likewise reports missing
known history within its window as PARTIAL. BACKFILL reports whether at least the
configured 150 trading observations were returned, without extending the start
or fabricating history. A nonempty shortfall is PARTIAL. A valid empty response is
NO_DATA with target/overlap warnings as applicable; no market-closure reason is
invented without evidence. NO_DATA_CHANGE remains a distinct state.

Per-security states are SUCCESS, PARTIAL, NO_DATA, NO_DATA_CHANGE, REJECTED,
PROVIDER_ERROR and DATA_QUALITY_ERROR. Run aggregation prioritizes FAIL over
PARTIAL over NO_DATA; only all SUCCESS/NO_DATA_CHANGE results yield PASS. All
non-PASS outcomes return exit code 2. Successful securities in a failed run remain
in development JSONL with their individual lineage; the run log must always be
checked before a downstream consumer treats a file as complete.

## Development outputs and evidence

Acquisition requires a fresh output directory and a run log directly inside it.
Existing directories are rejected to protect prior run evidence and prevent
pre-existing output links from overwriting source files. Direct or hard-linked
run-log aliases of input/config/contract are rejected in dry-run mode too.
`observations.jsonl` is created exclusively. The input is opened read-only and
SHA-256 is checked before/after execution on success and failure.

Run JSON retains Phase 1 fields and adds engine version, canonical plan hash,
provider, development contract hash, final input SHA-256, output path/hash, and
per-security evidence: full approved plan, acquisition mode, exact request bounds,
actual returned window where parseable, raw returned count and accepted normalized
count, provider/result status, HTTP status, retry count, retrieval time, expected
and actual old overlap counts, BACKFILL target achievement, warnings and failure
reason. Admission evidence retains explicit window approvals and run decisions;
the development decision source is also recorded at run level.
Configuration hash binds generator config, provider settings, contract
hash and sequential execution. Source commit, analysis date, input hash, elapsed
time and peak RSS remain included. Peak RSS uses Linux process-wide high-water
memory rather than an isolated per-security measurement.

Preserved provider metadata includes request epoch boundaries, HTTP body SHA-256
and byte size, and Yahoo's raw `meta` object (capped at 8 KiB). Price arrays and full
raw HTTP bodies are not written into evidence; arbitrary response headers and
error descriptions are not logged. A schema or transport failure may have an
unknown returned count/window. A quality rejection still records a parseable
raw count/window while emitting no accepted rows.

## Validation commands

```bash
python -m pytest -ra
ruff check .
python -m compileall -q r01_generator tests conftest.py
git diff --check
```

The mocked suite covers READY/BLOCKED/NO_DATA_CHANGE, inclusive/exclusive epoch
boundaries and Tokyo/UTC dates, parser and missing-value behavior, calendar gaps,
valid empty responses, schema errors, retry success/exhaustion and permanent 4xx,
quality failures, BACKFILL shortfalls, FULL_REFRESH evidence, batch failure,
input preservation, safe output locations, new-security contracts, conservative
request spacing and offline safety. The original 665-security/96-observation
regression remains unchanged and required.

## Limitations and Phase 2B handoff

Yahoo Chart is an unofficial, availability-dependent development source: no SLA,
provider-specific history limits, missing optional fields, no independent security
master verification, and no guaranteed adjustment/corporate-action completeness.
No live Yahoo verification was performed for this implementation. A user must
explicitly opt in and permit `query1.finance.yahoo.com` in a restricted network
before live acquisition. No production approval is inferred from mock tests.

Without an exchange-calendar oracle and per-security listing/suspension evidence,
Phase 2A cannot prove every future trading date is present or explain a legitimate
empty response as a holiday. SUCCESS means the requested response passed known
history/field/boundary checks, not a certified complete formal dataset. Final
analysis-date bars may be intraday; use a completed-market analysis date for
intentional development runs. FULL_REFRESH covers only its explicitly approved
window, without claiming inception coverage. Evidence retains the windows for
review. Prices with optional missing adjusted close/volume remain explicitly
marked and require a downstream policy before formal writing.

Phase 2B can consume the typed results/JSONL and plan/contract hashes to add a
reviewed trading-calendar/completeness policy, real-R01 integration measurements,
selective XLSX writing/rebuild, corporate actions and quality calculations.
Writers must independently validate run status, lineage, counts and input hashes;
PARTIAL/NO_DATA/FAIL must not silently publish. Formal quality_summary updates,
R01/R02, Google Drive, Current Baseline, TA/TB, Standards, Manifest, release/cutover,
and automatic trading remain outside this change. No unapproved fallback exists.
