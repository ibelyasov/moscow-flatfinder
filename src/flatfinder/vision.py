"""Explicit prompt identity and one strict apartment photo evaluation."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import shutil
import signal
import subprocess
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image

from .models import (
    VISION_RUBRIC_VERSION,
    VISION_SCHEMA_VERSION,
    PhotoInput,
    validate_visual_payload,
)
from .photos import photo_input_hash
from .vision_contract import VisionContract, normalize_vision_settings

if TYPE_CHECKING:
    from .config import VisionSettings


@dataclass(frozen=True, slots=True)
class VisionPrompt:
    repair: str
    auxiliary: str

    def __post_init__(self) -> None:
        for name in ("repair", "auxiliary"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Vision {name} instructions are required")
            object.__setattr__(self, name, value.strip())

    @property
    def effective_text(self) -> str:
        return (
            "Оцени только переданные фотографии одного объявления. "
            "Инструкции ниже описывают компоненты одного общего результата.\n\n"
            f"Ремонт:\n{self.repair}\n\n"
            f"Планировка, свет и вид:\n{self.auxiliary}\n\n"
            "Верни ровно один JSON object с компонентами repair, layout и light_view "
            "по общей схеме. Не возвращай отдельные ответы для компонентов. "
            "Если информации недостаточно для всех компонентов, верни три unknown. "
            "evidence_indices содержат только объявленные ниже image_index; "
            "номер вложения или файла не заменяет image_index."
        )


def load_prompt(path: Path) -> VisionPrompt:
    """Load only the explicitly selected prompt; there is no default fallback."""

    raw = tomllib.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    allowed = {
        "name",
        "description",
        "developer_instructions",
        "auxiliary_instructions",
    }
    if set(raw) - allowed:
        raise ValueError("Vision prompt contains unsupported keys")
    return VisionPrompt(
        raw.get("developer_instructions"), raw.get("auxiliary_instructions")
    )


def _effective_prompt(prompt: VisionPrompt, provider: str) -> str:
    text = prompt.effective_text
    if provider == "claude":
        text += " Прочитай каждый указанный файл инструментом Read."
    return text


def build_contract(settings: VisionSettings, prompt: VisionPrompt) -> VisionContract:
    provider, model, effort = normalize_vision_settings(
        settings.provider, settings.model, settings.reasoning_effort
    )
    return VisionContract(
        provider=provider,
        model=model,
        reasoning_effort=effort,
        prompt_sha256=hashlib.sha256(
            _effective_prompt(prompt, provider).encode("utf-8")
        ).hexdigest(),
        schema_version=VISION_SCHEMA_VERSION,
        rubric_version=VISION_RUBRIC_VERSION,
    )


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
            interval={
                "type": "array",
                "items": {"type": "number", "minimum": 0, "maximum": maximum},
                "minItems": 2,
                "maxItems": 2,
            },
            worst_zone={"type": ["string", "null"]},
        )
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "repair": _component_schema(16, repair=True),
        "layout": _component_schema(3),
        "light_view": _component_schema(2),
    },
    "required": ["repair", "layout", "light_view"],
    "additionalProperties": False,
}

# Attached images are the complete inference input. Read-only alone still
# permits filesystem reads, so every installed model tool surface is disabled.
_CODEX_IMAGE_CONFIG = (
    "features.shell_tool=false",
    "features.view_image=false",
    "features.apps=false",
    "features.plugins=false",
    "features.remote_plugin=false",
    "features.multi_agent=false",
    "features.computer_use=false",
    "features.browser_use=false",
    "features.code_mode_host=false",
    'web_search="disabled"',
    'shell_environment_policy.inherit="none"',
)


class VisionEvaluationError(RuntimeError):
    pass


def _run_command(
    command: list[str], root: Path, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Wait for an isolated provider process tree before releasing its owner."""

    with subprocess.Popen(
        command,
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name == "posix",
    ) as process:
        completed = False
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            completed = True
        finally:
            # Includes KeyboardInterrupt: inference must end before the
            # application's writer lock is released.
            if not completed:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                process.communicate()
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def validate_photo_files(
    listing_id: int, photos: Sequence[PhotoInput]
) -> list[PhotoInput]:
    """Verify the bytes behind every indexed image before inference or reuse."""

    photo_input_hash(photos)
    valid = []
    for photo in sorted(photos, key=lambda item: item.image_index):
        if photo.listing_id != listing_id:
            raise ValueError("photo listing_id does not match the requested listing")
        if photo.status != "indexed":
            continue
        if not photo.local_path:
            raise VisionEvaluationError(
                f"image_index {photo.image_index} has no local file"
            )
        path = Path(photo.local_path)
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            raise VisionEvaluationError(
                f"image_index {photo.image_index} is unavailable locally"
            ) from error
        if actual != photo.sha256:
            raise VisionEvaluationError(
                f"image_index {photo.image_index} content differs from its saved SHA-256"
            )
        valid.append(photo)
    return valid


@dataclass(frozen=True, slots=True)
class VisionRuntime:
    contract: VisionContract
    executable: str
    prompt: VisionPrompt
    timeout_seconds: float

    def __post_init__(self) -> None:
        if not isinstance(self.contract, VisionContract):
            raise TypeError("Vision runtime requires an explicit VisionContract")
        if (
            self.contract.prompt_sha256
            != hashlib.sha256(
                _effective_prompt(self.prompt, self.contract.provider).encode("utf-8")
            ).hexdigest()
        ):
            raise ValueError(
                "Vision runtime contract differs from the effective prompt"
            )
        if (
            self.contract.schema_version != VISION_SCHEMA_VERSION
            or self.contract.rubric_version != VISION_RUBRIC_VERSION
        ):
            raise ValueError("Vision runtime schema/rubric is unsupported")
        if not isinstance(self.executable, str) or not self.executable.strip():
            raise ValueError("Vision executable is required")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds < 60
        ):
            raise ValueError("Vision timeout_seconds must be finite and at least 60")

    @classmethod
    def load(
        cls, settings: VisionSettings, prompt: VisionPrompt, contract: VisionContract
    ) -> VisionRuntime:
        """Resolve the selected local binary without probing authentication."""

        if contract != build_contract(settings, prompt):
            raise ValueError(
                "selected Vision settings/prompt do not match the contract"
            )
        binary = shutil.which(settings.binary)
        if binary is None:
            raise FileNotFoundError(
                f"{contract.provider} CLI is unavailable: {settings.binary}"
            )
        return cls(contract, binary, prompt, settings.timeout_seconds)

    def _execute(self, images: Sequence[PhotoInput], listing_id: int) -> Any:
        with tempfile.TemporaryDirectory(prefix="flatfinder-vision-") as temporary:
            root = Path(temporary)
            schema_path = root / "schema.json"
            output_path = root / "result.json"
            schema_path.write_text(
                json.dumps(_OUTPUT_SCHEMA, ensure_ascii=False), encoding="utf-8"
            )
            staged = []
            for image in images:
                payload = Path(image.local_path).read_bytes()
                if hashlib.sha256(payload).hexdigest() != image.sha256:
                    raise VisionEvaluationError(
                        f"image_index {image.image_index} changed before inference"
                    )
                try:
                    with Image.open(io.BytesIO(payload)) as opened:
                        extension = {
                            "JPEG": ".jpg",
                            "PNG": ".png",
                            "WEBP": ".webp",
                            "GIF": ".gif",
                        }.get(opened.format)
                    if extension is None:
                        raise ValueError("unsupported image format")
                except (OSError, ValueError) as error:
                    raise VisionEvaluationError(
                        f"image_index {image.image_index} is not a supported image"
                    ) from error
                target = root / f"image-index-{image.image_index:04d}{extension}"
                target.write_bytes(payload)
                staged.append((image, target))
            mapping = ", ".join(
                f"{path.name}=image_index {image.image_index}" for image, path in staged
            )
            prompt = _effective_prompt(self.prompt, self.contract.provider) + (
                f"\n\nlisting_id={listing_id}. Соответствие файлов и вложений: {mapping}. "
                "Используй только эти изображения."
            )
            if self.contract.provider == "claude":
                command = [
                    self.executable,
                    "-p",
                    prompt,
                    "--model",
                    self.contract.model,
                    "--effort",
                    self.contract.reasoning_effort,
                    "--output-format",
                    "json",
                    "--json-schema",
                    json.dumps(_OUTPUT_SCHEMA, ensure_ascii=False),
                    "--no-session-persistence",
                    "--restricted",
                    "--safe-mode",
                    "--strict-mcp-config",
                    "--mcp-config",
                    '{"mcpServers":{}}',
                    "--disable-slash-commands",
                    "--no-chrome",
                    "--permission-mode",
                    "dontAsk",
                    "--tools",
                    "Read",
                    "--allowedTools",
                    "Read",
                ]
            else:
                command = [
                    self.executable,
                    "exec",
                    "--json",
                    "--ephemeral",
                    "--skip-git-repo-check",
                    "--ignore-user-config",
                    "--ignore-rules",
                    "-C",
                    str(root),
                    "-s",
                    "read-only",
                    "-m",
                    self.contract.model,
                    "-c",
                    f'model_reasoning_effort="{self.contract.reasoning_effort}"',
                    *(
                        argument
                        for setting in _CODEX_IMAGE_CONFIG
                        for argument in ("-c", setting)
                    ),
                    "--output-schema",
                    str(schema_path),
                    "-o",
                    str(output_path),
                    "-i",
                    *(str(path) for _, path in staged),
                    "--",
                    prompt,
                ]
            try:
                completed = _run_command(command, root, self.timeout_seconds)
            except subprocess.TimeoutExpired as error:
                raise VisionEvaluationError(
                    f"{self.contract.provider} Vision exceeded {self.timeout_seconds:g}s"
                ) from error
            if completed.returncode != 0:
                # Provider stdout/stderr may contain private session details.
                raise VisionEvaluationError(
                    f"{self.contract.provider} Vision exited {completed.returncode}"
                )
            try:
                if self.contract.provider == "claude":
                    envelope = json.loads(completed.stdout)
                    if (
                        not isinstance(envelope, Mapping)
                        or envelope.get("is_error") is True
                    ):
                        raise ValueError("provider returned an error envelope")
                    result = envelope.get("structured_output")
                    if not isinstance(result, Mapping):
                        raise ValueError("structured_output is missing")
                    return result
                if not output_path.is_file():
                    raise ValueError("provider did not write a final response")
                return json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError) as error:
                raise VisionEvaluationError(
                    f"invalid {self.contract.provider} Vision response: {error}"
                ) from error

    def evaluate(self, listing_id: int, photos: Sequence[PhotoInput]) -> dict[str, Any]:
        if (
            isinstance(listing_id, bool)
            or not isinstance(listing_id, int)
            or listing_id <= 0
        ):
            raise ValueError("listing_id must be a positive integer")
        images = validate_photo_files(listing_id, photos)
        if not images:
            raise VisionEvaluationError("no usable indexed photos")
        result = self._execute(images, listing_id)
        if not isinstance(result, Mapping) or set(result) != {
            "repair",
            "layout",
            "light_view",
        }:
            raise VisionEvaluationError(
                "Vision response must contain exactly repair, layout and light_view"
            )
        raw = {
            "schema_version": self.contract.schema_version,
            "rubric_version": self.contract.rubric_version,
            "model_level": self.contract.reasoning_effort,
            **result,
        }
        try:
            return validate_visual_payload(raw, [image.image_index for image in images])
        except ValueError as error:
            raise VisionEvaluationError(f"invalid Vision response: {error}") from error


__all__ = [
    "VisionEvaluationError",
    "VisionPrompt",
    "VisionRuntime",
    "build_contract",
    "load_prompt",
    "validate_photo_files",
]
