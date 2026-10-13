from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
import json
from pathlib import Path
import resource
import time
import uuid

from . import __version__


@dataclass
class RunContext:
    input_filename: str
    input_sha256: str
    config_hash: str
    analysis_date: date
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    snapshot_id: str | None = None
    source_commit: str | None = None
    generator_name: str = "R01 StockFacts Generator"
    generator_version: str = __version__
    execution_timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    mode_counts: dict[str, int] = field(default_factory=dict)
    blocked_count: int = 0
    validation_result: str = "NOT_RUN"
    elapsed_seconds: float = 0.0
    peak_rss_mb: float = 0.0
    _started: float = field(default_factory=time.monotonic, repr=False)

    def finish(self) -> None:
        self.elapsed_seconds = round(time.monotonic() - self._started, 6)
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        self.peak_rss_mb = round(rss / 1024, 3)  # Linux ru_maxrss is KiB.

    def write_json(self, path: str | Path) -> None:
        data = asdict(self)
        data.pop("_started", None)
        data["analysis_date"] = self.analysis_date.isoformat()
        Path(path).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
