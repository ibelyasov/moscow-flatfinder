# MoscowFlatFinder — Codex

При настройке пользовательского поиска полностью следуй
`docs/agent-onboarding.md`. Личные данные и runtime-файлы хранятся только вне Git
в `~/Library/Application Support/MoscowFlatFinder`. Не запускай полный сбор,
массовый Vision refresh или расписание без явного подтверждения пользователя.

## Разработка

- Do not add automated test files, test frameworks, or test dependencies to this project.
- Verify changes with focused temporary/inline smoke checks plus the repository checks documented in `README.md`: `compileall`, production-module imports, CLI smoke checks, SQLite integrity, and `git diff --check`.
- Do not commit temporary verification scripts or fixtures.

## Agent skills

### Issue tracker

Задачи и спецификации ведутся в GitHub Issues.
Перед работой с ними прочитай `docs/agents/issue-tracker.md`.

### Triage labels

Используются стандартные пять меток.
Перед triage прочитай `docs/agents/triage-labels.md`.

### Domain docs

Single-context: корневой `CONTEXT.md` и `docs/adr/`.
Перед исследованием кода прочитай `docs/agents/domain.md`.
