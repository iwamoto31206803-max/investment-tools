# R01 StockFacts Generator — Phase 1

## Status and purpose

Version **0.1.0-dev** is a development-only, read-only implementation. It is not
an Approved Release and performs no production cutover. It provides an executable
skeleton, a ZIP/XML R01 reader, a per-security acquisition planner, and JSON run
evidence. Git is the executable-source history; no Formal R01/R02, Drive, TA/TB,
Standard, Manifest, Current Baseline, or Weekly Run is changed.

## Architecture boundary

The R01 artifact contains observed facts. The generator reproducibly implements
TA/T03 meaning/rule/quality requirements, while its run log records tool, config,
input, plan, validation, timing, and memory evidence. ZIP/XML and streaming are
implementation details here and are not meaning specifications.

Python 3.11 is the primary target and there are no runtime dependencies outside
the standard library. XLSX is opened with `zipfile`; workbook relationships map
logical sheet names to components. XML rows are consumed with `iterparse` and
cleared immediately. `stock_daily` retains only each security's sorted unique
observation-date index—not cell objects, complete price facts, or a DataFrame.
Components are never extracted and the source file is opened read-only.

## CLI and configuration

```bash
python -m r01_generator.cli --input R01.xlsx \
  --config config/r01_generator.default.toml --dry-run \
  --analysis-date 2026-09-18 --output-run-log run.json
```

`--dry-run` is mandatory because Phase 1 has no writer. Optional `--run-id` and
`--snapshot-id` populate evidence. Configuration is strictly parsed, canonically
serialized as sorted compact JSON, and SHA-256 hashed. The 35-observation maximum
is explicitly a **development/shadow-run operational safety parameter**, not a
Current Standard.

## Modes and sanity gate

* `OVERLAP`: sufficient existing history; start is the Nth actual observation
  counting back from that security's latest observation (20 by default).
* `BACKFILL`: a new security or one with fewer than N observations; target 150.
* `FULL_REFRESH`: allowed only with a non-empty explicit reason.
* `NO_DATA_CHANGE`: available only when the caller explicitly supplies that
  run-level evidence decision.

An OVERLAP whose expected observations exceed the configured maximum is blocked
unless structured override evidence supplies reason, decision source, original
and override windows, and timestamp. Validation errors, exceptions, and blocked
plans are distinct; the CLI returns nonzero for `FAIL` or `BLOCKED` and still
writes evidence.

## Testing

Run `python -m pytest`. Tests build tiny package-level XLSX fixtures without
openpyxl. They cover eight-sheet relationship resolution, metadata, inventory,
history indexing, modes, fail-closed paths, input preservation, config hashing,
run evidence, and the permanent 665-security/96-observation regression.

## Known limitations and next phase

Phase 1 does not call Yahoo or any provider, estimate future market observations,
read a universe to add brand-new symbols in the CLI, implement provider fallback,
write `stock_daily`, rebuild XLSX, recalculate quality, update corporate actions,
or produce/upload formal production evidence. mmap is represented in configuration
for future tuning; the safe streaming parser is used now because ZIP members are
compressed streams. Phase 2 can add acquisition adapters, explicit trading-calendar
estimation, selective writing/rebuild, quality calculation, and measured real-R01
integration without changing TA/T03 semantics.
