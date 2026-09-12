"""Canonical, serializable scoring policy normalization and metadata."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from math import isfinite
from numbers import Real
from typing import Any

from .scoring import MAX_SCORES, normalized_max_scores, normalized_scoring_parameters
from .vision_contract import VisionContractLike
from .vision_contract import vision_contract as resolve_vision_contract

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

CRITERION_METADATA: dict[str, dict[str, str]] = {
    "noise": {"label": "Тишина", "help": "0–6 по ночной модели транспортного риска: достаточно одного близкого источника. Радиусы — 183 м для автодороги и 632 м для тяжёлой ЖД с OSM-поправками по классу; это screening, не расчёт дБ."},
    "park": {"label": "Парк и прогулки", "help": "Ближайший парк берётся из данных об окружении объявления. До 10 минут пешком сохраняется максимум; затем балл плавно снижается до 0 к 25 минутам."},
    "equipment": {"label": "Оснащение и мебель", "help": "Кровать, кондиционер, посудомойка, холодильник и стиральная машина — по 3 за подтверждённое наличие, без бонуса за полный комплект."},
    "repair": {"label": "Ремонт", "help": "Фотооценка ремонта по выбранной Vision-модели; неполные фото расширяют диапазон."},
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
    "visual_layout": {"label": "Планировка по фото", "help": "Vision оценивает удобство и свободную циркуляцию по фото."},
    "floor": {"label": "Этаж", "help": "Промежуточный этаж — 2; последний — 1; первый или неизвестный — 0."},
    "light_view": {"label": "Свет и вид", "help": "Vision оценивает свет и вид по фотографиям; без подходящих фото — 0."},
    "building": {"label": "Год дома", "help": "Только год постройки: 2020+ — 2; 2010-е — 1,5; 2000-е — 1; 1980–1999 — 0,5; раньше — 0; неизвестно — 1."},
    "personal": {"label": "Хочу здесь жить", "help": "Ваша оценка от 0 до 10."},
    "fitness": {"label": "Зал с сауной", "help": "Один поиск 2ГИС в радиусе 2 км. Качество по рейтингу и числу отзывов: обычный зал — до 2, хороший без сауны — до 4, хороший с явно указанной сауной — до 6. После 10 минут балл плавно снижается до 0 к 25 минутам."},
}


def _number(value: Any, path: str) -> float:
    if not isinstance(value, Real) or isinstance(value, bool):
        raise ValueError(f"{path} must be a finite number")
    number = float(value)
    if not isfinite(number):
        raise ValueError(f"{path} must be a finite number")
    return number


def _normalize_thresholds(values: Mapping[str, float] | None) -> dict[str, float]:
    result = dict(DEFAULT_THRESHOLDS)
    if values is not None:
        unknown = sorted(set(values) - set(result))
        if unknown:
            raise ValueError(f"unknown scoring thresholds: {', '.join(unknown)}")
        for name, value in values.items():
            number = _number(value, f"scoring.thresholds.{name}")
            if number < 0:
                raise ValueError(f"scoring.thresholds.{name} must be >= 0")
            result[name] = number
    if not result["priority"] >= result["good"] >= result["reserve"]:
        raise ValueError("scoring thresholds must satisfy priority >= good >= reserve")
    return result


def _normalize_hard_constraints(values: Mapping[str, Any] | None) -> dict[str, Any]:
    if values is None:
        return {}
    unknown = sorted(set(values) - SUPPORTED_HARD_CONSTRAINTS)
    if unknown:
        raise ValueError(f"unknown hard constraints: {', '.join(unknown)}")
    result: dict[str, Any] = {}
    for name in sorted(values):
        value = values[name]
        if name == "required_equipment":
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item.strip() for item in value
            ):
                raise ValueError("hard_constraints.required_equipment must be an array of names")
            result[name] = [item.strip() for item in value]
            continue
        result[name] = _number(value, f"hard_constraints.{name}")
    return result


def _normalize_vision_contract(
    value: VisionContractLike | None,
    *,
    use_default: bool,
) -> list[str] | None:
    if value is None and not use_default:
        return None
    return list(resolve_vision_contract(value).as_tuple())


def normalize_policy(
    *,
    max_scores: Mapping[str, float] | None = None,
    parameters: Mapping[str, float] | None = None,
    thresholds: Mapping[str, float] | None = None,
    hard_constraints: Mapping[str, Any] | None = None,
    vision_scoring_enabled: bool = False,
    vision_contract: VisionContractLike | None = None,
) -> dict[str, Any]:
    """Return the complete deterministic policy used for one assessment."""

    maxima = normalized_max_scores(max_scores)
    if not vision_scoring_enabled:
        for criterion in VISUAL_CRITERIA:
            maxima[criterion] = 0.0
    policy = {
        "max_scores": maxima,
        "parameters": normalized_scoring_parameters(parameters),
        "thresholds": _normalize_thresholds(thresholds),
        "hard_constraints": _normalize_hard_constraints(hard_constraints),
        "vision_scoring_enabled": bool(vision_scoring_enabled),
        "vision_contract": _normalize_vision_contract(
            vision_contract, use_default=bool(vision_scoring_enabled)
        ),
    }
    policy["fingerprint"] = policy_fingerprint(policy)
    return policy


def policy_fingerprint(
    policy: Mapping[str, Any] | None = None,
    *,
    max_scores: Mapping[str, float] | None = None,
    parameters: Mapping[str, float] | None = None,
    thresholds: Mapping[str, float] | None = None,
    hard_constraints: Mapping[str, Any] | None = None,
    vision_scoring_enabled: bool = False,
    vision_contract: VisionContractLike | None = None,
) -> str:
    """Hash a normalized policy, excluding any existing fingerprint."""

    if policy is None:
        policy = normalize_policy(
            max_scores=max_scores,
            parameters=parameters,
            thresholds=thresholds,
            hard_constraints=hard_constraints,
            vision_scoring_enabled=vision_scoring_enabled,
            vision_contract=vision_contract,
        )
    payload = deepcopy(dict(policy))
    payload.pop("fingerprint", None)
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def criterion_metadata(
    parameters: Mapping[str, float] | None = None,
) -> dict[str, dict[str, str]]:
    """Return labels and help rendered from the effective score parameters."""

    configured = normalized_scoring_parameters(parameters)
    return {
        name: {
            "label": metadata["label"],
            "help": metadata["help"].format(**configured),
        }
        for name, metadata in CRITERION_METADATA.items()
        if name in MAX_SCORES
    }
