# Конфигурация

Локальный `config.toml` создаётся из `automation/config.example.toml` и хранится
в `~/Library/Application Support/MoscowFlatFinder`. Checked-in example содержит
весь поддерживаемый публичный контракт; неизвестные scoring criteria и hard
constraints отклоняются.

## Capability

Core включён всегда. `[capabilities]` отдельно включает `geo`, `noise` и
`vision`. Связанные scoring criteria автоматически получают максимум 0 и
исчезают из результата, когда capability или Vision scoring выключены.

Geo требует destination и ключ 2GIS. Noise требует локальный map-файл. Vision
требует выбранный CLI и prompt-файл.

## Scoring

`[scoring.max_points]` задаёт максимум каждого фиксированного критерия. Ноль
отключает критерий. Итоговый максимум — сумма включённых критериев; проценты и
перераспределение веса не используются.

`[scoring.thresholds]` задаёт абсолютные границы `priority`, `good`, `reserve`.
После изменения capability или maxima пороги нужно пересчитать.

`[scoring.parameters]` меняет только опорные значения фиксированных формул:

- полную стоимость с максимумом и нулём;
- число месяцев амортизации комиссии;
- оценки коммунальных платежей;
- лучшее и нулевое время в дороге;
- стартовую, хорошую и полную площадь.

## Hard constraints

Поддерживаются:

- `max_monthly_total`;
- `min_area_m2`;
- `min_floor`;
- `max_commute_minutes`;
- `min_repair_score`;
- `required_equipment`.

Отсутствующий факт даёт `needs_review`, явное нарушение — `rejected`. Ограничение
на дорогу требует Geo, на ремонт — включённые Vision и Vision scoring. Прочие
условия остаются ручными и описываются в `search-profile.md`.

## Vision contract

`[vision]` задаёт `provider`, `model`, `reasoning_effort`, CLI и timeout. Есть
только два адаптера: `codex` и `claude`. Рекомендован
`codex / gpt-5.6-luna / medium`; другая модель разрешена с предупреждением о
несопоставимости.

Каждый run хранит provider, model, effort и prompt version. Изменение любого
элемента делает прошлый run не текущим. `--refresh-vision` и `vision --force`
остаются явными операциями.

## Пути и секреты

Все значения `[paths]` разрешаются относительно `runtime_dir`, а не checkout.
Search URL, destination, SQLite, photos, exports, logs и browser profile не
синхронизируются через Git.

Ключ 2GIS сначала читается из macOS Keychain по service/account. Поле
`geo.twogis_api_key` — локальный ignored fallback.
