# investment-tools

Development tooling for reproducible investment-data artifacts.

**Development only. Production Not Yet Approved.** R01 StockFacts Generator
Phase 2A adds opt-in Yahoo acquisition behind the preserved Phase 1 planner and
sanity gate. It reads R01 XLSX without modifying it and produces development JSON
evidence and normalized JSONL. Phase 2B adds an opt-in selective writer to a new
development XLSX. It does not upload to Google Drive, update formal artifacts,
or make trading decisions.

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

Phase 2B supports complete OVERLAP replacement/insertion and byte-identical
NO_DATA_CHANGE copies. Add `--output-xlsx /tmp/r01-development-run-001/updated.xlsx`
and `--writer-source-id <existing-source_registry-ID>` to the acquisition command.
The existing registry ID must be supplied explicitly; no source definitions are
created. Failed/incomplete batches publish no XLSX. Outputs must be new paths in
the development directory; the input is never overwritten.

Only stock_daily XML is materialized, one row at a time with disk-backed duplicate
keys. Other ZIP component contents remain byte-identical (ZIP compression may
change). Updated prices and provenance come from typed acquisition results;
unavailable adjusted close/volume stay blank. Updated adjusted OHLC, adjustment
factor and quality flags are cleared rather than retaining stale derived values.
Other seven sheets, including quality_summary, remain unchanged. Formal quality
is **not recalculated**; the new workbook is development-only and comes with
`updated.xlsx.writer.json` evidence plus the CLI run log. BACKFILL/FULL_REFRESH,
stock_daily formulas/tables/merged cells/extensions are unsupported for updates
and fail closed. NO_DATA_CHANGE copies the input bytes without XML rewriting.

Run a complete offline demonstration (no live transport is constructed):

```bash
python scripts/demo_phase2b_selective_writer.py \
  --development-dir /tmp/r01-phase2b-demo-001
```

This creates a synthetic input, a selectively updated XLSX, a NO_DATA_CHANGE XLSX,
normalized JSONL and acquisition/writer evidence. See the
[short execution report](docs/development/PHASE2B_SELECTIVE_WRITER_REPORT.md).
The approximately 20 MB accepted workbook has not been benchmarked; this design
avoids workbook-wide cell objects and retains the existing Reader date indexes
and shared-string handling. Production approval and formal quality calculations
remain outside this MVP.

Synthetic integration is reproducible without live network access using the supplied
fixture generator and `scripts/run_phase2a_synthetic_integration.py`.
See the [development integration report](docs/development/PHASE2A_SYNTHETIC_INTEGRATION_REPORT.md)
for commands, the 8725/7181 results, expected rejection/failure cases, and limitations.
These results do not establish real-R01 performance or successful live Yahoo acquisition.
