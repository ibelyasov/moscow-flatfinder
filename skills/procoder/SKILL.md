---
name: procoder
description: >-
  Work like a senior developer in a repository governed by procoder: run the
  commit gate before calling anything done, format and lint through the
  binary, and drive the spec, plan, todo, backlog, and sprint chain in
  .procoder/. Use this skill when the repository contains a .procoder/
  directory or an AGENTS.md naming procoder, or when the user asks to run the
  gate, check formatting, open a spec or plan, close a task, or prepare a
  release.
license: Apache-2.0
metadata:
  category: development
  author: pascal-watteel
---

# MoscowFlatFinder — Codex

При настройке пользовательского поиска полностью следуй
`docs/agent-onboarding.md`. Личные данные и runtime-файлы хранятся только вне Git
в `~/Library/Application Support/MoscowFlatFinder`. Не запускай полный сбор,
массовый Vision refresh или расписание без явного подтверждения пользователя.

## Разработка

- Do not add automated test files, test frameworks, or test dependencies to this project.
- Verify changes with focused temporary/inline smoke checks plus the repository checks documented in `README.md`: `compileall`, production-module imports, CLI smoke checks, SQLite integrity, and `git diff --check`.
- Do not commit temporary verification scripts or fixtures.
