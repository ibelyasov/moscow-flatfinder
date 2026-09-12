"""Stable identity used to match persisted Vision results."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypeAlias

from .models import VISION_RUBRIC_VERSION


@dataclass(frozen=True, slots=True)
class VisionContract:
    """Fields that make a persisted Vision run current for stored content."""

    provider: str
    model_name: str
    reasoning_effort: str
    prompt_version: str

    def __post_init__(self) -> None:
        for name in (
            "provider",
            "model_name",
            "reasoning_effort",
            "prompt_version",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Vision contract {name} must be a non-empty string")

    def as_tuple(self) -> tuple[str, str, str, str]:
        return (
            self.provider,
            self.model_name,
            self.reasoning_effort,
            self.prompt_version,
        )


VisionContractTuple: TypeAlias = tuple[str, str, str, str]
VisionContractLike: TypeAlias = VisionContract | VisionContractTuple

DEFAULT_VISION_CONTRACT = VisionContract(
    provider="codex",
    model_name="gpt-5.6-luna",
    reasoning_effort="medium",
    prompt_version=VISION_RUBRIC_VERSION,
)
PRODUCTION_PASS_CRITERIA = {"visual": ("owner_visual_assessment",)}
PRODUCTION_PASSES = tuple(PRODUCTION_PASS_CRITERIA)


def vision_contract(value: VisionContractLike | Sequence[str] | None) -> VisionContract:
    """Normalize typed and legacy four-item tuple contracts."""

    if value is None:
        return DEFAULT_VISION_CONTRACT
    if isinstance(value, VisionContract):
        return value
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) != 4
    ):
        raise ValueError(
            "Vision contract must contain provider, model, effort and prompt"
        )
    return VisionContract(*(str(item) for item in value))


__all__ = [
    "DEFAULT_VISION_CONTRACT",
    "PRODUCTION_PASSES",
    "PRODUCTION_PASS_CRITERIA",
    "VisionContract",
    "VisionContractLike",
    "VisionContractTuple",
    "vision_contract",
]
