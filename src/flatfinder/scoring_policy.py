"""Canonical policy assembled once by the application, with calculation identity."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from .models import EQUIPMENT_NAMES
from .scoring import (
    MAX_SCORES,
    _number,
    normalized_max_scores,
    normalized_scoring_parameters,
)
from .vision_contract import VisionContract

CALCULATION_VERSION = "listing-assessment-v1"
DEFAULT_THRESHOLDS = {"priority": 80.0, "good": 70.0, "reserve": 60.0}
SUPPORTED_HARD_CONSTRAINTS = frozenset(
    {
        "max_monthly_total",
        "min_area_m2",
        "min_floor",
        "max_commute_minutes",
        "min_repair_score",
        "required_equipment",
    }
)
VISUAL_CRITERIA = frozenset({"repair", "visual_layout", "light_view"})
_POLICY_KEYS = frozenset(
    {
        "max_scores",
        "parameters",
        "thresholds",
        "hard_constraints",
        "vision_scoring_enabled",
        "vision_contract",
        "measurement_context",
        "calculation_version",
        "fingerprint",
    }
)

CRITERION_METADATA: dict[str, dict[str, str]] = {
    "noise": {
        "label": "Тишина",
        "help": "0–6 по ночной модели транспортного риска: достаточно одного близкого источника. Радиусы — 183 м для автодороги и 632 м для тяжёлой ЖД с OSM-поправками по классу; это screening, не расчёт дБ.",
    },
    "park": {
        "label": "Парк и прогулки",
        "help": "Ближайший парк берётся из данных об окружении объявления. При неизвестной площади парка quality=0,5 — явное допущение; до 10 минут пешком сохраняется оценка; затем балл плавно снижается до 0 к 25 минутам.",
    },
    "equipment": {
        "label": "Оснащение и мебель",
        "help": "Мебель, кондиционер, посудомойка, холодильник и стиральная машина — по 3 за подтверждённое наличие, без бонуса за полный комплект.",
    },
    "repair": {
        "label": "Ремонт",
        "help": "Фотооценка ремонта по выбранной Vision-модели; неполные фото расширяют диапазон.",
    },
    "price": {
        "label": "Полная стоимость",
        "help": "Аренда + коммуналка + 1/{commission_amortization_months:g} комиссии. До {price_best_monthly_total:g} — максимум; затем smoothstep плавно снижает оценку до 0 на {price_zero_monthly_total:g}.",
    },
    "commute": {
        "label": "Дорога",
        "help": "Среднее door-to-door: максимум до {commute_best_minutes:g} мин, 0 на {commute_zero_minutes:g} мин и дальше; между опорами — интерполяция.",
    },
    "area": {
        "label": "Площадь",
        "help": "Рост начинается с {area_start_m2:g} м², основная опора {area_good_m2:g} м², максимум с {area_full_m2:g} м².",
    },
    "visual_layout": {
        "label": "Планировка по фото",
        "help": "Vision оценивает удобство и свободную циркуляцию по фото.",
    },
    "floor": {
        "label": "Этаж",
        "help": "Промежуточный этаж — 2; последний — 1; первый или неизвестный — 0.",
    },
    "light_view": {
        "label": "Свет и вид",
        "help": "Vision оценивает свет и вид по фотографиям; без подходящих фото — 0.",
    },
    "building": {
        "label": "Год дома",
        "help": "Только год постройки: 2020+ — 2; 2010-е — 1,5; 2000-е — 1; 1980–1999 — 0,5; раньше — 0; неизвестно — оценочное допущение 1 с partial confidence.",
    },
    "personal": {"label": "Хочу здесь жить", "help": "Ваша оценка от 0 до 10."},
    "fitness": {
        "label": "Зал с сауной",
        "help": "Один поиск 2ГИС в радиусе 2 км. Неподтверждённое качество даёт частичную оценку. Качество по рейтингу и числу отзывов: обычный зал — до 2, хороший без сауны — до 4, хороший с явно указанной сауной — до 6. После 10 минут балл плавно снижается до 0 к 25 минутам.",
    },
}


def _object(values: Mapping[str, Any] | None, name: str) -> Mapping[str, Any]:
    if values is None:
        return {}
    if not isinstance(values, Mapping) or any(
        not isinstance(key, str) for key in values
    ):
        raise ValueError(f"{name} must be an object with text keys")
    return values


def _normalize_thresholds(values: Mapping[str, float] | None) -> dict[str, float]:
    values = _object(values, "scoring.thresholds")
    unknown = sorted(set(values) - set(DEFAULT_THRESHOLDS))
    if unknown:
        raise ValueError(f"unknown scoring thresholds: {', '.join(unknown)}")
    result = dict(DEFAULT_THRESHOLDS)
    for name, value in values.items():
        number = _number(value)
        if number is None or number < 0:
            raise ValueError(f"scoring.thresholds.{name} must be a finite number >= 0")
        result[name] = number
    if not result["priority"] >= result["good"] >= result["reserve"]:
        raise ValueError("scoring thresholds must satisfy priority >= good >= reserve")
    return result


def _normalize_hard_constraints(values: Mapping[str, Any] | None) -> dict[str, Any]:
    values = _object(values, "hard_constraints")
    unknown = sorted(set(values) - SUPPORTED_HARD_CONSTRAINTS)
    if unknown:
        raise ValueError(f"unknown hard constraints: {', '.join(unknown)}")
    result = {}
    for name in sorted(values):
        value = values[name]
        if name == "required_equipment":
            if not isinstance(value, list) or any(
                not isinstance(item, str) or item not in EQUIPMENT_NAMES
                for item in value
            ):
                raise ValueError(
                    "hard_constraints.required_equipment must contain only furnished, ac, dishwasher, fridge, washer"
                )
            if len(value) != len(set(value)):
                raise ValueError(
                    "hard_constraints.required_equipment names must be unique"
                )
            result[name] = sorted(value)
            continue
        number = _number(value)
        if number is None or number < 0:
            raise ValueError(
                f"hard_constraints.{name} must be a finite nonnegative number"
            )
        if (
            name in {"max_monthly_total", "min_area_m2", "max_commute_minutes"}
            and number <= 0
        ):
            raise ValueError(f"hard_constraints.{name} must be greater than 0")
        if name == "min_floor" and (number < 1 or not number.is_integer()):
            raise ValueError("hard_constraints.min_floor must be a positive integer")
        if name == "max_commute_minutes" and number > 1440:
            raise ValueError("hard_constraints.max_commute_minutes must be <= 1440")
        if name == "min_repair_score" and number > MAX_SCORES["repair"]:
            raise ValueError("hard_constraints.min_repair_score must be inside [0,16]")
        result[name] = number
    return result


def _measurement_context(value: str | None) -> str | None:
    if value is not None and (
        not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
    ):
        raise ValueError(
            "measurement_context must be a lowercase SHA256 digest or None"
        )
    return value


def normalize_policy(
    *,
    max_scores: Mapping[str, float] | None = None,
    parameters: Mapping[str, float] | None = None,
    thresholds: Mapping[str, float] | None = None,
    hard_constraints: Mapping[str, Any] | None = None,
    vision_scoring_enabled: bool = False,
    vision_contract: VisionContract | None = None,
    measurement_context: str | None = None,
) -> dict[str, Any]:
    """Return deterministic policy; a config base may lack an effective contract."""
    if not isinstance(vision_scoring_enabled, bool):
        raise TypeError("vision_scoring_enabled must be a boolean")
    if vision_contract is not None and not isinstance(vision_contract, VisionContract):
        raise TypeError("vision_contract must be VisionContract or None")
    maxima = normalized_max_scores(max_scores)
    if not vision_scoring_enabled:
        for name in VISUAL_CRITERIA:
            maxima[name] = 0.0
    policy = {
        "max_scores": maxima,
        "parameters": normalized_scoring_parameters(parameters),
        "thresholds": _normalize_thresholds(thresholds),
        "hard_constraints": _normalize_hard_constraints(hard_constraints),
        "vision_scoring_enabled": vision_scoring_enabled,
        "vision_contract": vision_contract.to_dict()
        if vision_contract is not None
        else None,
        "measurement_context": _measurement_context(measurement_context),
        "calculation_version": CALCULATION_VERSION,
    }
    policy["fingerprint"] = policy_fingerprint(policy)
    return policy


def policy_fingerprint(policy: Mapping[str, Any]) -> str:
    if not isinstance(policy, Mapping):
        raise TypeError("policy_fingerprint requires a policy object")
    payload = {name: value for name, value in policy.items() if name != "fingerprint"}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_policy(policy: Mapping[str, Any], *, effective: bool = False) -> None:
    """Validate a supplied canonical policy without filling defaults or rebuilding it."""
    if not isinstance(policy, Mapping) or set(policy) != _POLICY_KEYS:
        raise ValueError("canonical policy keys are invalid")
    _measurement_context(policy["measurement_context"])
    if policy["calculation_version"] != CALCULATION_VERSION:
        raise ValueError("policy calculation version is unsupported")
    if not isinstance(policy["vision_scoring_enabled"], bool):
        raise TypeError("policy vision_scoring_enabled must be a boolean")
    maxima = policy["max_scores"]
    parameters = policy["parameters"]
    if (
        not isinstance(maxima, Mapping)
        or set(maxima) != set(MAX_SCORES)
        or normalized_max_scores(maxima) != dict(maxima)
    ):
        raise ValueError("canonical policy maxima are invalid")
    if (
        not isinstance(parameters, Mapping)
        or set(parameters) != set(normalized_scoring_parameters())
        or normalized_scoring_parameters(parameters) != dict(parameters)
    ):
        raise ValueError("canonical policy parameters are invalid")
    if _normalize_thresholds(policy["thresholds"]) != policy["thresholds"]:
        raise ValueError("canonical policy thresholds are invalid")
    if (
        _normalize_hard_constraints(policy["hard_constraints"])
        != policy["hard_constraints"]
    ):
        raise ValueError("canonical policy hard constraints are invalid")
    contract_payload = policy["vision_contract"]
    if contract_payload is not None:
        contract = VisionContract.from_dict(contract_payload)
        if contract.to_dict() != contract_payload:
            raise ValueError("policy Vision contract must be canonical")
    if effective and policy["vision_scoring_enabled"] and contract_payload is None:
        raise ValueError("effective Vision policy requires its execution contract")
    if not policy["vision_scoring_enabled"] and any(
        maxima[name] != 0 for name in VISUAL_CRITERIA
    ):
        raise ValueError("disabled Vision policy must have zero visual maxima")
    if not isinstance(policy["fingerprint"], str) or policy[
        "fingerprint"
    ] != policy_fingerprint(policy):
        raise ValueError("policy fingerprint does not match its contents")


def criterion_metadata(
    parameters: Mapping[str, float] | None = None,
) -> dict[str, dict[str, str]]:
    configured = normalized_scoring_parameters(parameters)
    return {
        name: {
            "label": metadata["label"],
            "help": metadata["help"].format(**configured),
        }
        for name, metadata in CRITERION_METADATA.items()
    }
