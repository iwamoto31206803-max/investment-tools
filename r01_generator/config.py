from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class GeneratorConfig:
    overlap_observation_target: int = 20
    overlap_max_without_override: int = 35
    backfill_target: int = 150
    provider_retry_count: int = 3
    mmap_enabled: bool = True
    mmap_max_xml_size: int = 1_073_741_824
    streaming_fallback_enabled: bool = True
    validation_profile: str = "development"

    @classmethod
    def load(cls, path: str | Path) -> "GeneratorConfig":
        with Path(path).open("rb") as stream:
            values = tomllib.load(stream)
        unknown = set(values) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown configuration keys: {sorted(unknown)}")
        config = cls(**values)
        if min(config.overlap_observation_target, config.overlap_max_without_override,
               config.backfill_target, config.provider_retry_count) < 1:
            raise ValueError("numeric configuration values must be positive")
        return config

    def canonical_bytes(self) -> bytes:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True).encode()

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()
