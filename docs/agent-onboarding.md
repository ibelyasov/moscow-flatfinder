# Агентский onboarding MoscowFlatFinder

Канонический сценарий настройки личного поиска для Codex и Claude Code.
Публичные примеры — в `examples/`, исполняемый код — в `src/flatfinder`.
Подробный контракт настроек: [configuration.md](configuration.md).

## Правила агента

- Задавай по одному вопросу, объясняй последствия и рекомендуй вариант.
- Не придумывай бюджет, критерии, районы, destination или coverage bounds.
- Личные config, search-profile, URL, адреса, SQLite, photos, browser state,
  exports и scheduler-файлы хранятся только вне Git в runtime. Не создавай
  symlink из checkout и не печатай секреты или частные URL в ответы/логи/issues.
- Login/CAPTCHA/2FA проходит пользователь; ограничения площадок не обходятся.
- Полный сбор, provider-операции, массовый Vision refresh, расписание,
  map rebuild/download, live migration/switch, credentials и backup/restore/prune
  требуют явного разрешения в рамках текущей задачи.
- Конфигурация — источник исполняемых настроек, `search-profile.md` объясняет
  решения. Произвольное пожелание не превращается в неподдерживаемый hard constraint.

## 1. Подготовь окружение

Проверь Apple Silicon macOS, Python 3.12–3.14 и подходящий `uv`. Установи root
проект; Superset setup делает только этот шаг:

```sh
uv sync --locked
```

Для разрешённого browser-сбора Chromium устанавливается отдельной командой:

```sh
uv run --locked playwright install chromium
```

Создай частный runtime `~/Library/Application Support/MoscowFlatFinder` с
`browser-profile`, `data`, `exports`, `photos`. Скопируй туда без перезаписи
существующего профиля:

- `examples/config.toml` → `config.toml`;
- `examples/vision-prompt.toml` → `vision-prompt.toml`;
- `examples/search-profile.md` → `search-profile.md`.

Если уже существует schema 17, остановись перед переключением runtime.
Нормальное приложение её не мигрирует. Подготовь reviewable план отдельного
офлайн-импорта в новую schema 18: readonly source, новый target, технический
архив всех старых строк и явный photo-root для сохранения ссылок. Реальную базу,
фото, backup или config не меняй без согласия. Потерянную хронологию старого
формата восстановить нельзя; подробнее в configuration.md.

Завершение: зависимости доступны, частный профиль сохранён вне checkout;
существующие данные не заменены.

## 2. Проведи интервью

Отдели обязательные условия от предпочтений. Зафиксируй бюджет и полную месячную
стоимость, площадь/этаж, оснащение, дорогу и явное время отправления, важность
парков/зала/тишины/ремонта/света/планировки, ручные критерии, допустимые расходы
Geo/Vision и источники (Яндекс Недвижимость, ЦИАН или оба).

Покажи таблицу: критерий, `hard`/`score`/`manual`, источник доказательства,
максимум и поведение при неизвестном значении. Предположения о коммунальных
платежах и других расходах имеют partial confidence, влияют на мягкий рейтинг и
не подтверждают hard budget. `furnished` не означает наличие конкретной кровати.
Все предложения сохраняют отдельные личные решения даже при duplicate links.

Завершение: пользователь подтвердил критерии и предположения в частном профиле.

## 3. Исследуй районы и получи search URL

Подбери небольшой стартовый набор районов/метро по согласованным критериям.
Для каждого запиши аргументы за/против и проверки конкретного дома; проверенные
факты отделяй от гипотез. Точный destination не попадает в Git.

Пользователь проверяет фильтры в браузере и подтверждает каждый URL перед
записью в `[[searches]]`. Не обещай эквивалентность фильтров разных площадок.
Для разрешённого login используй:

```sh
uv run --locked flatfinder --config "/path/to/private/config.toml" login
```

Завершение: подтверждённые поиски записаны, пользователь завершил нужный login;
это ещё не доказательство успешного сбора.

## 4. Настрой capability и scoring

Включи только выбранные `geo`, `noise`, `vision`. Geo требует destination,
service/account в Keychain либо явный ключ в частном config, день недели и время
отправления в обе стороны. Хранение/изменение credentials согласуется отдельно.
Noise требует совместимую v2 карту с объявленными полными границами; её построение
и загрузка — отдельная операция с optional extra `noise`, не часть общего setup.

Для Vision пользователь выбирает provider/model/effort и допустимые расходы.
По умолчанию `auto_accept = false`: результат pending до решения человека.
`scoring_enabled` разрешает баллы только от подходящего принятого результата.
Автоматическое принятие включай только при явном выборе пользователя.

Пересчитай maxima и абсолютные thresholds. Core-пример — 39 automatic +
10 personal = 49 total. Не добавляй неподдерживаемые ключи или старые aliases.
Глобальный `--config` всегда стоит перед командой.

Завершение: config и search-profile выражают одни решения, capability и hard
constraints согласованы, приватные файлы не отслеживаются Git.

## 5. Проверь локальную готовность

Для нового runtime явно создай schema 18, затем проверь doctor:

```sh
uv run --locked flatfinder --config "/path/to/private/config.toml" init
uv run --locked flatfinder --config "/path/to/private/config.toml" doctor
```

Не выполняй `init` поверх имеющейся базы. Для подготовленного импорта используй
его новый target только после разрешённого переключения config.
Doctor не запускает browser/provider/inference, не читает Keychain и не
мигрирует schema. Он проверяет локальные файлы и контракты; наличие CLI не
доказывает authentication. При `ok = false` или ненулевом exit code исправь
локальную причину; provider readiness проверяй отдельно в разрешённом запуске.

Завершение: doctor возвращает `ok = true`, ограничения его проверки понятны.

## 6. Проведи разрешённый ограниченный запуск

Согласуй маленький `[collection].max_listings` и один источник, затем запусти:

```sh
uv run --locked flatfinder --config "/path/to/private/config.toml" run
uv run --locked flatfinder --config "/path/to/private/config.toml" review --port 8765
```

Проверь сохранённые facts/assessments, denominator отключённых capability,
`eligible`/`needs_review`/`rejected`, таблицу/карту/карточку, текущую галерею и
pending Vision. Blocker должен завершаться fail-closed; успешные записи остаются
сохранёнными. Discovery повторяется после restart, membership разных поисков
не смешивается. Review держит writer lock только во время мутаций, а не всю
жизнь сервера; UI доступен на loopback.

При включённом Vision подтвердите или отклоните pending run в UI либо через
`vision-review RUN_ID --accept`/`--reject`. Отдельное `vision ID --force` и
`run --refresh-vision` повторяют inference. Проверяй currentness после смены
prompt/модели/фото. При новом профиле scoring выполняй `reassess [ID]` без
провайдеров; для новых Geo/Noise измерений используй `enrich [ID] [--force]`.

Завершение: ограниченная выборка проверена, ошибки и неизмеренные capability
зафиксированы; частные данные остались вне Git. Второй источник проверяется
отдельно, если выбран.

## 7. Уточни профиль, полный запуск и расписание

Покажи результаты и уточни районы, maxima, thresholds и формулы. Обнови частный
config/profile. Только после подтверждения верни рабочий лимит и выполни полный
сбор; проверь JSON, SQLite integrity и UI. Это отдельный слой acceptance от
development checks и doctor.

Расписание настраивается только после успешного ручного запуска и явного
разрешения. Выбери доступный runner (Codex/Claude, Hermes, launchd); конфиг
расписания хранится вне Git. CLI сама получает файловый writer lock на операцию.
Не удаляй предыдущий checkout/runtime. Collect не делает automatic backup:
согласованные `backup` и optional `backup --keep N` выполняются отдельно.

Завершение: расписание использует проверенную команду, частные пути и согласованный
лимит; runtime switch, backup/prune и массовый Vision не объявлены выполненными
по одним локальным проверкам.
