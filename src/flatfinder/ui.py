"""Streamlit review of saved observations; writes use application operations."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pydeck as pdk
import streamlit as st

from flatfinder.application import (
    current_policy,
    reassess,
    record_review,
    review_vision,
    unlink_duplicate,
)
from flatfinder.config import Config, load_config, parse_listing_id
from flatfinder.database import Database
from flatfinder.photos import is_allowed_photo_url
from flatfinder.read_model import dashboard_payload

_METRO_DATA = Path(__file__).with_name("assets") / "moscow_metro.json"
_STATUS_LABELS = {
    "priority": "Приоритет",
    "good": "Хороший вариант",
    "reserve": "Запасной вариант",
    "skip": "Пропустить",
}


def _number(value: Any) -> str:
    return "—" if value is None else f"{value:g}"


def _text(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _field(item: dict[str, Any], name: str) -> Any:
    field = item["fields"].get(name)
    return field["value"] if field is not None else None


def _heading(item: dict[str, Any]) -> str:
    return str(_field(item, "address") or f"Объявление {item['id']}")


def _safe_link(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.username
        ):
            return value
    except ValueError:
        pass
    return None


def _photo_source(photo: dict[str, Any], root: Path) -> str | None:
    """Private local files must remain inside the configured photo directory."""
    local_path = photo.get("local_path")
    if isinstance(local_path, str) and local_path:
        try:
            candidate, directory = (
                Path(local_path).expanduser().resolve(),
                root.resolve(),
            )
            if directory in candidate.parents and candidate.is_file():
                return str(candidate)
        except (OSError, RuntimeError):
            pass
    url = photo.get("source_url")
    return url if isinstance(url, str) and is_allowed_photo_url(url) else None


def _read_view(config: Config, policy: dict[str, Any], listing_id: int | None):
    # Dashboard and selected detail share a consistent read transaction. No cache
    # keeps an old manual decision alive after a writer finishes.
    with Database(config.paths.database, readonly=True) as database:
        payload = dashboard_payload(
            database, policy, listing_id=listing_id, include_inactive=True
        )
        detail = next(
            (item for item in payload["listings"] if item["id"] == listing_id), None
        )
    return payload, detail


def _map_coordinates(item: dict[str, Any]) -> tuple[float, float] | None:
    point = _field(item, "location_point")
    if not isinstance(point, dict):
        return None
    lat, lon = point.get("lat"), point.get("lon")
    if (
        isinstance(lat, (int, float))
        and not isinstance(lat, bool)
        and isinstance(lon, (int, float))
        and not isinstance(lon, bool)
        and math.isfinite(lat)
        and math.isfinite(lon)
        and -90 <= lat <= 90
        and -180 <= lon <= 180
    ):
        return lat, lon
    return None


def _map_rows(items: list[dict[str, Any]], comparable: bool) -> list[dict[str, Any]]:
    rows = []
    for item in items:
        point = _map_coordinates(item)
        if point is None:
            continue
        maximum = item["rubric"]["total_max"]
        score = item.get("total_score")
        fraction = (
            min(1.0, max(0.0, score / maximum))
            if comparable and maximum and score is not None
            else None
        )
        color = (
            [107, 114, 128, 220]
            if fraction is None
            else [round(220 * (1 - fraction)), round(160 * fraction), 90, 220]
        )
        rows.append(
            {
                "id": item["id"],
                "position": [point[1], point[0]],
                "address": _heading(item),
                "color": color,
            }
        )
    return rows


def _metro_rows() -> list[dict[str, Any]]:
    payload = json.loads(_METRO_DATA.read_text(encoding="utf-8"))
    return [
        {
            "position": [station["lng"], station["lat"]],
            "name": station["name"],
            "color": list(bytes.fromhex(line["hex_color"])) + [200],
        }
        for line in payload["lines"]
        for station in line["stations"]
    ]


def _view_key(name: str, items: list[dict[str, Any]]) -> str:
    identity = ",".join(str(item["id"]) for item in items)
    digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return f"{name}-{st.session_state.get('view_epoch', 0)}-{digest}"


def _render_map(items: list[dict[str, Any]], comparable: bool) -> None:
    rows = _map_rows(items, comparable)
    if not rows:
        st.info("У объявлений нет сохранённых координат.")
        return
    show_metro = st.checkbox("Станции метро", value=False)
    layers = [
        pdk.Layer(
            "ScatterplotLayer",
            id="offers",
            data=rows,
            get_position="position",
            get_fill_color="color",
            get_radius=70,
            radius_min_pixels=7,
            pickable=True,
        )
    ]
    if show_metro:
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                id="metro",
                data=_metro_rows(),
                get_position="position",
                get_fill_color="color",
                get_radius=30,
                radius_min_pixels=3,
            )
        )
    event = st.pydeck_chart(
        pdk.Deck(
            layers=layers,
            map_provider="carto",
            initial_view_state=pdk.ViewState(
                longitude=sum(row["position"][0] for row in rows) / len(rows),
                latitude=sum(row["position"][1] for row in rows) / len(rows),
                zoom=11,
            ),
            tooltip={"text": "{address}"},
        ),
        key=_view_key("offers-map", items),
        on_select="rerun",
        selection_mode="single-object",
    )
    selected = event.selection.objects.get("offers", [])
    if selected:
        st.query_params["listing_id"] = selected[0]["id"]
        st.rerun()
    st.caption(
        "Карта использует сохранённые координаты. "
        + (
            "Цвет — доля от максимума оценки."
            if comparable
            else "Цвет нейтральный: оценки несопоставимы."
        )
    )


def _filter_measurement(
    value: Any, minimum: float | None, maximum: float | None
) -> bool:
    if minimum is None and maximum is None:
        return True
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and (minimum is None or value >= minimum)
        and (maximum is None or value <= maximum)
    )


def _numeric_range(
    label: str, key: str, step: float
) -> tuple[float | None, float | None]:
    st.sidebar.caption(label)
    lower, upper = st.sidebar.columns(2)
    minimum = lower.number_input(
        "От", min_value=0.0, value=None, step=step, key=f"{key}-min"
    )
    maximum = upper.number_input(
        "До", min_value=0.0, value=None, step=step, key=f"{key}-max"
    )
    if minimum is not None and maximum is not None and minimum > maximum:
        st.sidebar.error("Нижняя граница должна быть не выше верхней.")
    return minimum, maximum


def _render_table(items: list[dict[str, Any]], comparable: bool) -> None:
    rows = [
        {
            "ID": item["id"],
            "Источник": item["source"],
            "Адрес": _heading(item),
            "Аренда, ₽": _field(item, "price_monthly"),
            "Полная стоимость, ₽ (оценка)": item["estimated_monthly_total"],
            "Площадь, м²": _field(item, "area_m2"),
            "Дорога, мин": item["average_commute_minutes"],
            "Оценка": item["total_score"] if comparable else "Несопоставима",
            "Связанные объявления": len(item["duplicate_links"]),
            "Актуальна": not item["assessment_stale"],
            "В поиске": item["in_search"],
            "Доступность": item["availability"],
            "Избранное": item["favorite"],
            "Отклонено": item["disliked"],
        }
        for item in items
    ]
    # Incomparable scores are available in detail, never in a sortable column.
    event = st.dataframe(
        rows,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key=_view_key("offers-table", items),
    )
    if event.selection.rows:
        st.query_params["listing_id"] = items[event.selection.rows[0]]["id"]
        st.rerun()
    if not comparable:
        st.caption(
            "Сохранённые оценки доступны в карточках. Сортировка по общей оценке отключена."
        )


def _notice(warnings: list[str], message: str) -> None:
    st.session_state["flatfinder_notice"] = (message, warnings)
    st.rerun()


def _render_decisions(
    config: Config, item: dict[str, Any], rubric: dict[str, Any]
) -> None:
    maximum = float(rubric["personal_max"])
    current = float(item["personal_score"])
    with st.form(f"review-{item['id']}"):
        if maximum > 0:
            personal = st.number_input(
                "Хочу здесь жить",
                min_value=0.0,
                max_value=max(maximum, current),
                value=current,
                step=0.5,
                help=f"Текущий максимум: {_number(maximum)}",
            )
        else:
            personal = None
        favorite = st.checkbox("Избранное", value=item["favorite"])
        disliked = st.checkbox("Отклонить объявление", value=item["disliked"])
        submitted = st.form_submit_button("Сохранить решение")
    if submitted:
        changed_score = (
            personal if personal is not None and personal != current else None
        )
        if changed_score is not None and changed_score > maximum:
            st.error(f"Новая личная оценка не может превышать {_number(maximum)}.")
            return
        try:
            warnings = record_review(
                config,
                item["id"],
                personal_score=changed_score,
                favorited=favorite,
                disliked=disliked,
            )
        except Exception as exc:
            st.error(f"Решение не сохранено: {exc}")
        else:
            _notice(warnings, "Решение сохранено.")
    if st.button("Пересчитать по текущей политике", key=f"reassess-{item['id']}"):
        try:
            result = reassess(config, item["id"])
        except Exception as exc:
            st.error(f"Пересчёт не выполнен: {exc}")
        else:
            if result.blocked_reason:
                st.error(f"Пересчёт заблокирован: {result.blocked_reason}")
            elif result.failed:
                st.error("Пересчёт завершился с ошибками.")
                st.json(result.errors)
            else:
                _notice([], "Оценка пересчитана.")


def _render_scores(item: dict[str, Any], rubric: dict[str, Any]) -> None:
    st.subheader("Оценка")
    if item["assessment_stale"]:
        st.warning(
            "Нужны новые Geo/Noise-измерения. Простой пересчёт не обновляет данные. Используйте enrich для этого объявления."
            if item.get("measurement_stale")
            else "Сохранённая оценка устарела. Пересчитайте её по текущим фактам и политике."
        )
    assessment = item["assessment"]
    for name, criterion in rubric["criteria"].items():
        detail = assessment.get(name)
        if name == "personal":
            st.write(
                f"{criterion['label']}: {_number(item['personal_score'])}/{_number(criterion['max'])}"
            )
            continue
        if detail is None:
            st.write(f"{criterion['label']}: нет оценки")
            continue
        st.write(
            f"{criterion['label']}: {_number(detail['score'])}/{_number(criterion['max'])}"
        )
        st.caption(f"Уверенность: {detail['confidence']}")
        if detail.get("assumptions"):
            st.warning("Предположения: " + _text(detail["assumptions"]))
        for evidence in detail.get("evidence", []):
            st.caption(_text(evidence))
        if detail.get("details"):
            with st.expander(f"Подробности: {criterion['label']}"):
                st.json(detail["details"])
    eligibility = assessment.get("eligibility")
    if eligibility:
        st.write("Обязательные требования")
        st.json(eligibility)


def _render_photos(
    config: Config, item: dict[str, Any], evidence_indices: list[int] | None = None
) -> None:
    photos = {photo["image_index"]: photo for photo in item["photos"]}
    indices = evidence_indices if evidence_indices is not None else list(photos)
    for index in indices:
        photo = photos.get(index)
        if photo is None:
            st.warning(
                f"Фото #{index} отсутствует в текущем наборе; исторические индексы не переназначаются."
            )
            continue
        source = _photo_source(photo, config.paths.photos)
        if source:
            st.image(source, caption=f"Фото #{index}", width="stretch")
        else:
            st.caption(f"Фото #{index}: недоступно ({photo.get('status', 'unknown')})")


def _render_vision(
    config: Config, item: dict[str, Any], detail: dict[str, Any]
) -> None:
    run = (
        detail["vision"]["pending"]
        or detail["vision"]["accepted"]
        or detail["vision"]["current"]
        or detail["vision"]["latest"]
    )
    if run is None:
        return
    st.subheader("Фотооценка Vision")
    st.caption(f"Запуск {run['id']} · {run['status']}")
    latest = detail["vision"]["latest"]
    if latest is not None and latest["id"] != run["id"]:
        st.caption(f"Последний запуск: {latest['id']} · {latest['status']}")
        if latest.get("error"):
            st.warning(latest["error"])
    st.json(run["contract"])
    current = detail["vision"]["current"]
    if (
        current is None
        or current["contract"] != run["contract"]
        or run["input_hash"] != item["photo_input_hash"]
    ):
        st.warning(
            "Историческая фотооценка: она не соответствует текущим фотографиям или контракту Vision."
        )
    if run.get("error"):
        st.warning(run["error"])
    accepted = detail["vision"]["accepted"]
    if accepted is not None and accepted["id"] != run["id"]:
        st.caption(
            f"В оценке остаётся принятый запуск {accepted['id']} до нового решения."
        )
        with st.expander("Принятая фотооценка"):
            st.json(accepted["result"])
    result = run.get("result")
    if result:
        # Raw rubric values stay separate from scaled assessment contributions.
        st.caption("Исходная фотооценка; вклад в общую оценку показан выше.")
        st.json(result)
        indices = sorted(
            {
                index
                for name in ("repair", "layout", "light_view")
                for index in result[name]["evidence_indices"]
            }
        )
        if indices and run["input_hash"] != item["photo_input_hash"]:
            st.warning(
                "Фотооценка относится к прежнему набору фото. Текущие фотографии не подставляются вместо исторических."
            )
        elif indices:
            with st.expander("Фотографии, на которые ссылается Vision"):
                _render_photos(config, item, indices)
    if run["status"] == "pending":
        st.info(
            "Фотооценка ожидает вашего решения. Принятие действует только для текущих фото и контракта."
        )
        pending = detail["vision"]["pending"]
        can_accept = (
            pending is not None
            and pending["id"] == run["id"]
            and run["input_hash"] == item["photo_input_hash"]
        )
        if not can_accept:
            st.warning("Принятие отключено: фотографии или контракт Vision изменились.")
        accept, reject = st.columns(2)
        action = (
            True
            if accept.button(
                "Принять Vision", key=f"accept-{run['id']}", disabled=not can_accept
            )
            else (
                False
                if reject.button("Отклонить Vision", key=f"reject-{run['id']}")
                else None
            )
        )
        if action is not None:
            try:
                warnings = review_vision(config, run["id"], action)
            except Exception as exc:
                st.error(f"Решение Vision не сохранено: {exc}")
            else:
                _notice(warnings, "Решение Vision сохранено.")


def _render_duplicate_links(config: Config, detail: dict[str, Any]) -> None:
    links = detail["duplicate_links"]
    if not links:
        return
    st.subheader("Связанные объявления")
    st.caption("Каждое объявление сохраняет собственную оценку и личные решения.")
    for link in links:
        st.markdown(
            f"[Объявление {link['other_listing_id']}](?listing_id={link['other_listing_id']})"
        )
        st.caption(f"{link['method']} · {_text(link['confidence'])}")
        if link["evidence"]:
            st.caption(_text(link["evidence"]))
        left, right = link["left_listing_id"], link["right_listing_id"]
        if st.button("Удалить связь", key=f"unlink-{left}-{right}"):
            try:
                warnings = unlink_duplicate(config, left, right)
            except Exception as exc:
                st.error(f"Связь не удалена: {exc}")
            else:
                _notice(warnings, "Связь удалена.")


def _render_detail(
    config: Config,
    item: dict[str, Any],
    detail: dict[str, Any],
    rubric: dict[str, Any],
    current_rubric: dict[str, Any],
) -> None:
    st.title(_heading(item))
    st.caption(
        f"{item['source']} · {item['id']} · {item['availability']}"
        + (" · вне текущего поиска" if not item["in_search"] else "")
    )
    url = _safe_link(item["source_url"])
    if url:
        st.link_button("Открыть объявление", url)
    st.metric(
        "Сохранённая оценка",
        f"{_number(item['total_score'])}/{_number(rubric['total_max'])}",
    )
    _render_decisions(config, item, current_rubric)
    _render_scores(item, rubric)
    with st.expander("Сохранённые факты и их происхождение"):
        st.json(item["fields"])
    _render_duplicate_links(config, detail)
    _render_vision(config, item, detail)
    with st.expander("Текущие фотографии"):
        _render_photos(config, item)
    with st.expander("История наблюдений"):
        st.json(detail.get("observation_history", []))


def main() -> None:
    st.set_page_config(page_title="MoscowFlatFinder", page_icon="🏠", layout="wide")
    config_path = os.environ.get("FLATFINDER_CONFIG")
    if not config_path:
        st.error(
            "Откройте интерфейс через flatfinder review с выбранной конфигурацией."
        )
        return
    notice = st.session_state.pop("flatfinder_notice", None)
    if notice:
        st.success(notice[0])
        for warning in notice[1]:
            st.warning(warning)
    try:
        config = load_config(Path(config_path))
        policy = current_policy(config)
        listing_id = parse_listing_id(
            os.environ.get("FLATFINDER_LISTING_ID") or st.query_params.get("listing_id")
        )
        payload, detail = _read_view(config, policy, listing_id)
    except Exception as exc:
        st.error(f"Не удалось прочитать базу: {exc}")
        return
    items = payload["listings"]
    if listing_id is not None:
        if not os.environ.get("FLATFINDER_LISTING_ID") and st.button(
            "← Все объявления"
        ):
            st.query_params.clear()
            st.session_state["view_epoch"] = st.session_state.get("view_epoch", 0) + 1
            st.rerun()
        item = next((item for item in items if item["id"] == listing_id), None)
        if item is None or detail is None:
            st.info("Объявление не найдено.")
        else:
            _render_detail(config, item, detail, item["rubric"], payload["rubric"])
        return
    st.title("Квартиры к просмотру")
    st.caption("Все предложения независимы; связанные объявления не скрываются.")
    for warning in payload.get("warnings", []):
        st.warning(warning)
    if not items:
        st.info("В базе пока нет объявлений.")
        return
    comparable = payload["scores_comparable"] and not any(
        item["assessment_stale"] for item in items
    )
    if not comparable:
        st.warning(
            "Оценки устарели или рассчитаны по разным политикам. Ранжирование и цветовая шкала отключены."
        )
    min_total, max_total = _numeric_range(
        "Полная стоимость, ₽ (с предположениями)", "cost", 5000.0
    )
    min_area, max_area = _numeric_range("Площадь, м²", "area", 1.0)
    min_commute, max_commute = _numeric_range(
        "Среднее время в дороге, мин", "commute", 1.0
    )
    only_favorites = st.sidebar.checkbox("Только избранное")
    new_only = st.sidebar.checkbox("Только без личного решения")
    activity = st.sidebar.selectbox(
        "Участие в поиске",
        ["Все", "В текущем поиске", "Вне текущего поиска", "Недоступные"],
    )
    show_disliked = st.sidebar.checkbox("Показывать отклонённые", value=True)
    status = st.sidebar.selectbox("Статус оценки", ["Все", *_STATUS_LABELS.values()])
    items = [
        item
        for item in items
        if (not only_favorites or item["favorite"])
        and (show_disliked or not item["disliked"])
        and (status == "Все" or _STATUS_LABELS.get(item.get("status")) == status)
        and (not new_only or item["is_new"])
        and (
            activity == "Все"
            or activity == "В текущем поиске"
            and item["in_search"]
            or activity == "Вне текущего поиска"
            and not item["in_search"]
            or activity == "Недоступные"
            and item["availability"] == "unavailable"
        )
        and _filter_measurement(item["estimated_monthly_total"], min_total, max_total)
        and _filter_measurement(_field(item, "area_m2"), min_area, max_area)
        and _filter_measurement(
            item["average_commute_minutes"], min_commute, max_commute
        )
    ]
    if comparable and st.sidebar.checkbox("Сначала лучшие", value=True):
        items.sort(key=lambda item: (-item["total_score"], item["id"]))
    if not items:
        st.info("Нет объявлений по выбранным фильтрам.")
        return
    table, map_tab = st.tabs(["Таблица", "Карта"])
    with table:
        _render_table(items, comparable)
    with map_tab:
        _render_map(items, comparable)


if __name__ == "__main__":
    main()
