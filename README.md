# investment-tools

Development tooling for reproducible investment-data artifacts.

**Development only. Production Not Yet Approved.** R01 StockFacts Generator
Phase 2A adds opt-in Yahoo acquisition behind the preserved Phase 1 planner and
sanity gate. It reads R01 XLSX without modifying it and produces development JSON
evidence and normalized JSONL. It does not write XLSX, upload to Google Drive,
update formal artifacts, or make trading decisions.

- [Phase 1 reader/planner design](docs/R01_GENERATOR_DESIGN.md)
- [Phase 2A acquisition architecture and contract](docs/R01_ACQUISITION_DESIGN.md)

Python 3.11+ is required; runtime dependencies remain standard-library only.
Install the development tools in a virtual environment:

```bash
python3 -m venv /workspace/venvs/investment-tools
source /workspace/venvs/investment-tools/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -ra
ruff check .
```

Default execution is offline planning. Existing `--dry-run` invocations continue
to work, and neither invocation constructs an HTTP transport:

```bash
python -m r01_generator.cli --input /data/R01.xlsx \
  --config config/r01_generator.default.toml --dry-run \
  --analysis-date 2026-09-24 --output-run-log /tmp/r01-plan.json
```

Live acquisition requires explicit `--acquire`, an input-bound development
contract, and a **new** development output directory. Prepare the contract as
documented in the Phase 2A design, using the actual input SHA-256 and analysis
date; then explicitly invoke:

```bash
python -m r01_generator.cli --input /data/R01.xlsx \
  --config config/r01_generator.default.toml --acquire \
  --development-contract /data/development-contract.json \
  --development-dir /tmp/r01-development-run-001 \
  --analysis-date 2026-09-24 \
  --output-run-log /tmp/r01-development-run-001/run.json
```

The sole network destination is `query1.finance.yahoo.com` over verified HTTPS;
restricted cloud environments need to allow that hostname before an intentional
live run. No Yahoo credentials are configured. Automated tests forbid live
socket connections and use mocked HTTP fixtures. No live-provider validation is
implied by passing tests.

Requests use the approved inclusive Tokyo-date window and encode Yahoo's
exclusive end as midnight of the following day in Asia/Tokyo converted to UTC.
The adapter never expands windows to fill missing history. Requests run
sequentially with a one-second minimum interval and up to three retries after
the initial attempt, using bounded exponential backoff. Permanent 4xx and schema
errors do not retry. No fallback is configured.

`observations.jsonl` stores Decimal prices/volume as decimal strings and unavailable
values as JSON `null`; unadjusted `CLOSE` and `ADJUSTED_CLOSE` remain separate.
Every row contains acquisition lineage. Run evidence records raw returned count,
accepted normalized count, windows, hashes, provider metadata, retries, warnings,
and failures. Missing old overlap observations, required OHLC, or the BACKFILL
150-observation target yields `PARTIAL`; valid empty responses yield `NO_DATA`.
These outcomes return nonzero and cannot become `PASS`. Calendar gaps are retained
without inventing holiday rows or zero prices. See the design for validation and
limitations, including the absence of a trading-calendar completeness oracle.

Phase 2B can consume these development outputs after review to implement selective
writing/rebuild and formal quality calculations. Those capabilities and production
approval are outside Phase 2A.
