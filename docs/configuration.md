# Конфигурация

Текущий пример — [`examples/config.toml`](../examples/config.toml). Локальный
`config.toml` и runtime находятся вне Git, по умолчанию в
`~/Library/Application Support/MoscowFlatFinder`. Старые root/CLI aliases не
поддерживаются; неизвестные ключи, критерии и hard constraints отклоняются.

Глобальный `--config` ставится перед командой:

```sh
uv run --locked flatfinder --config "/path/to/private/config.toml" doctor
```

## Пути и сбор

`runtime_dir` разрешается относительно файла config, `[paths]` — относительно
runtime. Runtime должен быть вне checkout, все пути — внутри runtime и различны.
Поддерживаются только `database`, `export`, `noise_map`, `browser_profile`,
`photos`, `vision_prompt`, `search_profile`, `lock`.

`[collection]`: `max_listings`, `retries`, `headed`, `timeout_seconds`.
`[[searches]]` содержит только `url`. Playwright работает последовательно;
перезапуск повторяет discovery и сохраняет уже записанные результаты.
Завершённый поиск обновляет только свою membership; неполный поиск не подтверждает
исчезновение других предложений. Предложения независимы: duplicate links не
скрывают объявления и не переносят личные решения.

## Capability и Geo

Core включён всегда. `[capabilities]` явно включает `geo`, `noise`, `vision`;
по умолчанию они выключены. Зависимые критерии получают максимум 0, когда
capability или Vision scoring выключены. Core-пример имеет automatic/personal/
total maxima **39/10/49**.

`[geo]` задаёт `destination`, `twogis_api_key` либо пару
`keychain_service`/`keychain_account`, `min_interval_seconds`, `jitter_seconds`,
`timeout_seconds`, `departure_weekday` (0 — понедельник, 6 — воскресенье),
`to_work_time` и `to_home_time` в формате `HH:MM`. Время отправления — явное
предположение пользователя; подходящий день вычисляется для запуска. Для парка и
зала проверяется по одному ближайшему кандидату каждого вида.

Загрузка TOML, doctor и просмотр оценок не читают Keychain. Ключ разрешается
только для Geo-операции: сначала непустой `twogis_api_key`, иначе указанная пара
service/account. Обычная настройка оставляет ключ в Keychain; изменение credentials
требует отдельного согласия. Ключи, destination и search URL не печатаются в
публичные логи, issue или коммиты.

```sh
uv run --locked flatfinder --config "/path/to/private/config.toml" enrich 123
uv run --locked flatfinder --config "/path/to/private/config.toml" enrich 123 --force
```

Без ID команда обрабатывает активные объявления. `enrich` заменяет прежние
`retry-routes` и `refresh-coordinates`. `--force` игнорирует кеш измерений;
изменённые destination, время отправления или версия провайдера также меняют
идентичность входа. Это provider-операция, а не локальный пересчёт.

## Scoring и hard constraints

`[scoring.max_points]` задаёт максимум фиксированного критерия; 0 отключает его.
Знаменатель — сумма включённых максимумов, не обязательно 100.
`[scoring.thresholds]` задаёт абсолютные `priority`, `good`, `reserve`.
`[scoring.parameters]` содержит опорные значения формул стоимости, амортизации
комиссии, коммунальных платежей, дороги и площади; поддерживаемые имена перечислены
в примере. После смены capability или maxima пересчитайте пороги.

Оценки отсутствующих расходов — явные предположения с `partial` confidence.
Они влияют на мягкое ранжирование, но не подтверждают hard budget. Неизвестный
факт для обязательного условия даёт `needs_review`, подтверждённое нарушение —
`rejected`. `furnished` подтверждает мебель, но не конкретную кровать.

Поддерживаются только `max_monthly_total`, `min_area_m2`, `min_floor`,
`max_commute_minutes`, `min_repair_score`, `required_equipment`.
Дорога требует Geo, ремонт — Vision scoring. Прочие условия остаются ручными в
`search-profile.md`. Неполный ответ Geo не становится подтверждённым измерением.

Каждая оценка хранит нормализованную policy и fingerprint. Смена настроек или
эффективного Vision contract помечает сохранённую оценку как stale; разные
политики не сравниваются общей сортировкой по баллу. Пересчёт явный:

```sh
uv run --locked flatfinder --config "/path/to/private/config.toml" reassess
uv run --locked flatfinder --config "/path/to/private/config.toml" reassess 123
```

Он использует текущие сохранённые факты и подходящий принятый Vision, без
площадок, Geo или inference. Ручные оценки, избранное и dislike сохраняются.
Если личная оценка превышает новый максимум `personal`, операция сообщает ошибку,
а не обрезает решение. Сохраните прежний максимум или явно измените значение
командой `personal-score ID SCORE`. Ошибки отдельных объявлений дают ненулевой
exit code и не отменяют уже завершённые записи.

## Vision

`[vision]`: `provider` (`codex`/`claude`), `model`, `reasoning_effort`, `binary`,
`timeout_seconds`, `scoring_enabled`, `auto_accept`. Модель выбирает пользователь;
`examples/config.toml` содержит пример, а не доказательство качества или
аутентификации CLI. Prompt — [`examples/vision-prompt.toml`](../examples/vision-prompt.toml).

По умолчанию `auto_accept = false`: успешный результат остаётся pending до
решения в UI или CLI. Для включения автоматического принятия нужно явно выбрать
`auto_accept = true`. `scoring_enabled` отдельно разрешает вклад принятого
результата в рейтинг. Допустим результат, где все компоненты неизвестны.

```sh
uv run --locked flatfinder --config "/path/to/private/config.toml" vision 123
uv run --locked flatfinder --config "/path/to/private/config.toml" vision-review 456 --accept
uv run --locked flatfinder --config "/path/to/private/config.toml" vision-review 456 --reject
```

Последние две команды — альтернативные решения для ID pending run, не listing ID.
Для Codex отключены shell, чтение дополнительных изображений, web search, apps,
plugins, browser/computer use и multi-agent tools. Claude ограничен чтением
подготовленных фото в временном каталоге.

Contract включает provider, model, effort, SHA-256 эффективного prompt, schema и
rubric version; текущий вход также включает упорядоченные фото и их фактические
хеши. Смена prompt или фото исключает повторное использование старого результата.
`vision ID --force` и `run --refresh-vision` явно повторяют inference; массовый
refresh требует подтверждения. Установленный CLI и успешный doctor не доказывают
provider authentication.

## Noise map v2

Карта хранит schema/model version, SHA-256 исходного extract и явные
`coverage_bounds` вместе с `coverage_complete = true`. Несовместимая карта,
неполное покрытие или точка вне границ дают неизвестный результат, а не тишину.
Старую карту нужно отдельно пересобрать из полного OSM/PBF extract.

После согласования обновления установите optional build dependency:
`uv sync --locked --extra noise`. Затем укажите локальный extract либо явно
выбранный URL и его реальные полные границы:

```sh
uv run --locked flatfinder --config "/path/to/private/config.toml" refresh-noise-map --source /path/to/complete.osm.pbf --bounds WEST SOUTH EAST NORTH
```

Замените WEST/SOUTH/EAST/NORTH числовыми longitude/latitude границами extract.
Объявление complete coverage — ответственность владельца источника; приложение
не выводит полноту из найденных дорог. Map rebuild/download — отдельная
согласованная операция, не побочный эффект review или collect.

## SQLite, импорт и backups

Runtime открывает только текущую schema 18. `init` создаёт новую базу явно;
обычное открытие schema 17 отклоняется без миграции. Для офлайн-подготовки:

```sh
uv run --locked python tools/import_v17.py --source /path/to/closed-v17.sqlite3 --target /path/to/new-v18.sqlite3 --photo-root /path/to/same-runtime/photos
```

Source строго readonly и должен быть закрыт без pending WAL/journal. Target —
новый файл, публикуемый атомарно после проверки integrity/FK без перезаписи.
Сохраняются ID и ручные решения; текущий старый снимок выбирается по сохранённому
хешу. Исходные строки, описания, evidence, Geo/Vision/runs и прежние оценки
остаются в technical archive. Утраченная хронология повторных A→B→A наблюдений
не выдумывается. Policy импорта допускает все сохранённые личные баллы и сообщает
свой fingerprint; это не перенос частной конфигурации.

`--photo-root` необязателен и явно разрешает только ссылки внутри этого корня в
том же runtime. Без него старые пути/метаданные архивируются, а текущие фото
ожидают загрузки. Инструмент не читает, не копирует и не удаляет фото. Подготовка
новой базы не переключает live runtime; реальная миграция и смена config требуют
отдельного согласия. Для демо используется только `tools/generate_demo.py` без
входной базы или фотографий.

`backup` явно создаёт проверенную SQLite-копию в runtime/backups.
`backup --keep N` дополнительно удаляет старые копии после публикации новой.
Collect не создаёт и не prune-ит backups автоматически. Создание, restore и
prune требуют разрешения владельца; эти команды не входят в development checks.

Смена адреса назначения, времени поездок или содержимого Noise map меняет
контекст оценки. Старые оценки отмечаются stale; `enrich` получает новые
измерения, `reassess` только пересчитывает сохранённые факты. Связь дубликатов
можно удалить в карточке; ручное отклонение этой пары сохраняется при следующих
сборах. Оба объявления и их ручные решения остаются независимыми.
