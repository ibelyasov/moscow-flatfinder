"""Optional apartment photo scoring via the Codex or Claude CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomllib

from .models import (
    VISION_RUBRIC_VERSION,
    VISION_SCHEMA_VERSION,
    FullTextRecord,
    PhotoInput,
    ResultStatus,
    ReviewStatus,
    VisionProposal,
    validate_visual_payload,
)

MODEL_NAME = "gpt-5.6-luna"
DEFAULT_PROMPT_VERSION = VISION_RUBRIC_VERSION
PRODUCTION_PASS_CRITERIA = {"visual": ("owner_visual_assessment",)}
PRODUCTION_PASSES = tuple(PRODUCTION_PASS_CRITERIA)


def _component_schema(maximum: int, *, repair: bool = False) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "status": {"type": "string", "enum": ["scoreable", "unknown"]},
        "score": {"type": ["number", "null"], "minimum": 0, "maximum": maximum},
        "evidence_indices": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0},
        },
        "unknowns": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 160},
        },
        "summary": {"type": "string", "minLength": 1, "maxLength": 600},
    }
    if repair:
        properties.update(
            {
                "interval": {
                    "type": "array",
                    "items": {"type": "number", "minimum": 0, "maximum": maximum},
                    "minItems": 2,
                    "maxItems": 2,
                },
                "worst_zone": {"type": ["string", "null"]},
            }
        )
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_REPAIR_OUTPUT_SCHEMA = _component_schema(16, repair=True)

_AUXILIARY_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "layout": _component_schema(3),
        "light_view": _component_schema(2),
    },
    "required": ["layout", "light_view"],
    "additionalProperties": False,
}


class VisionEvaluationError(RuntimeError):
    pass


def _remap_attachment_indices(value: Any, images: Sequence[PhotoInput]) -> Any:
    """Translate Luna's occasional 1-based attachment positions to image_index."""

    result = json.loads(json.dumps(value, ensure_ascii=False))
    mapping = {position: image.image_index for position, image in enumerate(images, 1)}
    for name in ("repair", "layout", "light_view"):
        component = result.get(name) if isinstance(result, dict) else None
        indices = (
            component.get("evidence_indices") if isinstance(component, dict) else None
        )
        if isinstance(indices, list) and all(
            isinstance(index, int) and not isinstance(index, bool) and index in mapping
            for index in indices
        ):
            component["evidence_indices"] = [mapping[index] for index in indices]
    return result


@dataclass(slots=True)
class VisionRunResult:
    status: str
    proposals: list[VisionProposal] | None = None
    visual_coverage: float = 0.0
    schema_valid: bool = False
    retry_count: int = 0
    error: str | None = None

    def __post_init__(self) -> None:
        if self.proposals is None:
            self.proposals = []


@dataclass(slots=True)
class VisionRuntime:
    provider: str
    executable: str
    developer_instructions: str
    auxiliary_instructions: str
    timeout_seconds: int = 900
    model_name: str = MODEL_NAME
    model_version: str = MODEL_NAME
    reasoning_effort: str = "medium"
    prompt_version: str = DEFAULT_PROMPT_VERSION
    usage_events: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(
        cls,
        agent_config_path: str,
        *,
        provider: str = "codex",
        model_name: str = MODEL_NAME,
        reasoning_effort: str = "medium",
        codex_bin: str = "codex",
        claude_bin: str = "claude",
        timeout_seconds: int = 900,
    ) -> VisionRuntime:
        path = Path(agent_config_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Vision prompt does not exist: {path}")
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        instructions = raw.get("developer_instructions")
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError("Vision developer_instructions are required")
        auxiliary = raw.get("auxiliary_instructions")
        if not isinstance(auxiliary, str) or not auxiliary.strip():
            raise ValueError("Vision auxiliary_instructions are required")
        provider = str(provider).strip().lower()
        if provider not in {"codex", "claude"}:
            raise ValueError("vision provider must be codex or claude")
        if not str(model_name).strip():
            raise ValueError("vision model is required")
        reasoning_effort = str(reasoning_effort).strip().lower()
        if reasoning_effort not in {"minimal", "low", "medium", "high", "xhigh"}:
            raise ValueError(
                "vision reasoning_effort must be minimal, low, medium, high or xhigh"
            )
        configured_bin = codex_bin if provider == "codex" else claude_bin
        binary = shutil.which(str(configured_bin))
        if binary is None:
            raise FileNotFoundError(f"{provider} CLI is unavailable: {configured_bin}")
        auth_command = (
            [binary, "login", "status"]
            if provider == "codex"
            else [binary, "--version"]
        )
        try:
            auth = subprocess.run(
                auth_command,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"{provider} CLI check timed out") from exc
        auth_detail = f"{auth.stdout}\n{auth.stderr}"
        if auth.returncode != 0 or (
            provider == "codex" and "ChatGPT" not in auth_detail
        ):
            raise RuntimeError(f"{provider} CLI is not ready")
        if isinstance(timeout_seconds, bool) or int(timeout_seconds) < 60:
            raise ValueError("vision_timeout_seconds must be at least 60")
        return cls(
            provider,
            binary,
            instructions.strip(),
            auxiliary.strip(),
            int(timeout_seconds),
            str(model_name).strip(),
            str(model_name).strip(),
            reasoning_effort,
        )

    @staticmethod
    def _valid_images(images: Sequence[PhotoInput]) -> list[PhotoInput]:
        return [
            image
            for image in images
            if isinstance(image, PhotoInput)
            and image.status == "indexed"
            and image.local_path
            and image.duplicate_of is None
            and image.duplicate_of_index is None
            and Path(image.local_path).is_file()
        ]

    def _execute(
        self,
        images: Sequence[PhotoInput],
        prompt: str,
        schema: Mapping[str, Any],
        pass_name: str,
    ) -> Any:
        with tempfile.TemporaryDirectory(prefix="flatfinder-vision-") as temporary:
            root = Path(temporary)
            schema_path = root / "schema.json"
            output_path = root / "result.json"
            schema_path.write_text(
                json.dumps(schema, ensure_ascii=False), encoding="utf-8"
            )
            if self.provider == "claude":
                copied = []
                for position, image in enumerate(images, 1):
                    source = Path(str(image.local_path)).resolve()
                    target = root / f"image-{position}{source.suffix.lower()}"
                    shutil.copy2(source, target)
                    copied.append(target.name)
                image_note = "\n\nПрочитай изображения инструментом Read: " + ", ".join(
                    copied
                )
                command = [
                    self.executable,
                    "-p",
                    prompt + image_note,
                    "--model",
                    self.model_name,
                    "--output-format",
                    "json",
                    "--json-schema",
                    json.dumps(schema, ensure_ascii=False),
                    "--permission-mode",
                    "dontAsk",
                    "--tools",
                    "Read",
                    "--add-dir",
                    str(root),
                ]
            else:
                command = [
                    self.executable,
                    "exec",
                    "--json",
                    "--ephemeral",
                    "--skip-git-repo-check",
                    "--ignore-rules",
                    "-C",
                    str(root),
                    "-s",
                    "read-only",
                    "-m",
                    self.model_name,
                    "-c",
                    f'model_reasoning_effort="{self.reasoning_effort}"',
                    "--output-schema",
                    str(schema_path),
                    "-o",
                    str(output_path),
                    "-i",
                    *(str(Path(image.local_path).resolve()) for image in images),
                    "--",
                    prompt,
                ]
            try:
                completed = subprocess.run(
                    command,
                    cwd=root,
                    text=True,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise VisionEvaluationError(
                    f"Vision {pass_name} evaluation exceeded {self.timeout_seconds}s"
                ) from exc
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()[-1000:]
                raise VisionEvaluationError(
                    f"{self.provider} Vision {pass_name} exited {completed.returncode}: {detail or 'no output'}"
                )
            if self.provider == "claude":
                try:
                    envelope = json.loads(completed.stdout)
                    result = envelope.get("structured_output")
                    if result is None and isinstance(envelope.get("result"), str):
                        result = json.loads(envelope["result"])
                    if not isinstance(result, Mapping):
                        raise ValueError("structured_output is missing")
                    return result
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise VisionEvaluationError(
                        f"invalid Claude {pass_name} response: {exc}"
                    ) from exc
            if not output_path.is_file():
                raise VisionEvaluationError(
                    f"Codex Vision {pass_name} did not write a final response"
                )
            for line in completed.stdout.splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                usage = (
                    event.get("usage")
                    if event.get("type") == "turn.completed"
                    else None
                )
                if isinstance(usage, Mapping):
                    self.usage_events.append({"pass": pass_name, **dict(usage)})
            try:
                return json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise VisionEvaluationError(
                    f"invalid Vision {pass_name} response: {exc}"
                ) from exc

    def evaluate_visual(
        self, listing_id: int, images: Sequence[PhotoInput]
    ) -> VisionProposal:
        valid = self._valid_images(images)
        if not valid:
            raise VisionEvaluationError("visual: no usable indexed photos")
        mapping = ", ".join(
            f"attachment {position}=image_index {image.image_index}"
            for position, image in enumerate(valid, 1)
        )
        suffix = (
            f"\n\nТекущий listing_id={int(listing_id)}. Соответствие вложений: {mapping}. "
            "Оцени только этот listing и верни один JSON object."
        )
        repair = self._execute(
            valid,
            self.developer_instructions + suffix,
            _REPAIR_OUTPUT_SCHEMA,
            "repair",
        )
        auxiliary = self._execute(
            valid,
            self.auxiliary_instructions + suffix,
            _AUXILIARY_OUTPUT_SCHEMA,
            "auxiliary",
        )
        raw = {
            "schema_version": VISION_SCHEMA_VERSION,
            "rubric_version": DEFAULT_PROMPT_VERSION,
            "model_level": self.reasoning_effort,
            "repair": repair,
            **auxiliary,
        }
        try:
            value = validate_visual_payload(raw, [image.image_index for image in valid])
        except ValueError as exc:
            if "evidence_indices" not in str(exc):
                raise VisionEvaluationError(f"invalid Vision response: {exc}") from exc
            try:
                value = validate_visual_payload(
                    _remap_attachment_indices(raw, valid),
                    [image.image_index for image in valid],
                )
            except ValueError as remapped_exc:
                raise VisionEvaluationError(
                    f"invalid Vision response: {remapped_exc}"
                ) from remapped_exc
        scoreable = [
            value[name]
            for name in ("repair", "layout", "light_view")
            if value[name]["status"] == "scoreable"
        ]
        interval_width = float(value["repair"]["interval"][1]) - float(
            value["repair"]["interval"][0]
        )
        return VisionProposal(
            listing_id=int(listing_id),
            vision_run_id=0,
            pass_name="visual",
            criterion="owner_visual_assessment",
            value=value,
            confidence=round(
                (len(scoreable) / 3) * max(0.0, 1.0 - interval_width / 16.0), 3
            ),
            review_status=ReviewStatus.PENDING,
            result_status=ResultStatus.CATEGORY,
            model_name=self.model_name,
            model_version=self.model_version,
            prompt_version=self.prompt_version,
            image_indices=[image.image_index for image in valid],
            text_quotes=[],
            evidence=[item["summary"] for item in scoreable],
            conflicts=[],
        )

    def close(self) -> None:
        return None


def run_passes(
    runtime: VisionRuntime,
    listing_id: int,
    images: Sequence[PhotoInput],
    full_text: FullTextRecord,
    deterministic_facts: Mapping[str, Any],
    *,
    model_version: str,
    prompt_version: str,
) -> VisionRunResult:
    del deterministic_facts
    if int(full_text.listing_id) != int(listing_id):
        raise ValueError("full_text listing_id does not match listing_id")
    if (
        model_version != runtime.model_version
        or prompt_version != runtime.prompt_version
    ):
        raise ValueError("Vision runtime contract mismatch")
    try:
        proposal = runtime.evaluate_visual(listing_id, images)
    except VisionEvaluationError as exc:
        return VisionRunResult(status="failed", error=str(exc), schema_valid=False)
    coverage = (
        sum(
            1
            for name in ("repair", "layout", "light_view")
            if proposal.value
            and proposal.value.get(name, {}).get("status") == "scoreable"
        )
        / 3
    )
    return VisionRunResult(
        status="success",
        proposals=[proposal],
        visual_coverage=coverage,
        schema_valid=True,
    )


__all__ = [
    "DEFAULT_PROMPT_VERSION",
    "MODEL_NAME",
    "PRODUCTION_PASSES",
    "PRODUCTION_PASS_CRITERIA",
    "VisionEvaluationError",
    "VisionRunResult",
    "VisionRuntime",
    "run_passes",
]
