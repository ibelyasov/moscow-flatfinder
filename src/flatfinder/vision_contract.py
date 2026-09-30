"""One strict execution contract for current photo assessments."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from .models import VISION_RUBRIC_VERSION, VISION_SCHEMA_VERSION

_PROVIDER_EFFORTS = {
    "codex": frozenset({"minimal", "low", "medium", "high", "xhigh"}),
    "claude": frozenset({"low", "medium", "high", "xhigh", "max"}),
}


def normalize_vision_settings(
    provider: str, model: str, reasoning_effort: str
) -> tuple[str, str, str]:
    """Normalize explicit strings once; reject aliases and unsupported efforts."""
    values = {
        "provider": provider,
        "model": model,
        "reasoning_effort": reasoning_effort,
    }
    for name, value in values.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Vision {name} must be non-empty text")
    provider = provider.strip().lower()
    model = model.strip()
    reasoning_effort = reasoning_effort.strip().lower()
    if provider not in _PROVIDER_EFFORTS:
        raise ValueError("Vision provider must be codex or claude")
    if reasoning_effort not in _PROVIDER_EFFORTS[provider]:
        choices = ", ".join(sorted(_PROVIDER_EFFORTS[provider]))
        raise ValueError(
            f"Vision {provider} reasoning_effort must be one of: {choices}"
        )
    return provider, model, reasoning_effort


@dataclass(frozen=True, slots=True)
class VisionContract:
    provider: str
    model: str
    reasoning_effort: str
    prompt_sha256: str
    schema_version: str
    rubric_version: str

    def __post_init__(self) -> None:
        provider, model, effort = normalize_vision_settings(
            self.provider, self.model, self.reasoning_effort
        )
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "reasoning_effort", effort)
        if not isinstance(self.prompt_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.prompt_sha256
        ):
            raise ValueError("Vision prompt_sha256 must be a lowercase SHA256 digest")
        if (
            self.schema_version != VISION_SCHEMA_VERSION
            or self.rubric_version != VISION_RUBRIC_VERSION
        ):
            raise ValueError("Vision contract schema/rubric version is unsupported")

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> VisionContract:
        required = {
            "provider",
            "model",
            "reasoning_effort",
            "prompt_sha256",
            "schema_version",
            "rubric_version",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise ValueError("Vision contract keys are invalid")
        return cls(**dict(value))

    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


__all__ = ["VisionContract", "normalize_vision_settings"]
