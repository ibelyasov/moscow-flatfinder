# Issue tracker: GitHub

Задачи и спецификации живут в GitHub Issues репозитория
ibelyasov/moscow-flatfinder. Используй gh CLI.

Для многострочного текста используй временный файл и --body-file.
Операции выполняй в пределах авторизованной пользователем задачи.

## Pull requests as a triage surface

**PRs as a request surface: no.**

## Skill operations

- «Publish to the issue tracker»: создать GitHub issue.
- «Fetch the relevant ticket»: прочитать issue вместе с комментариями.
- Для wayfinder карта — issue с меткой wayfinder:map.
  Дочерние задачи связываются через sub-issues; если они недоступны,
  используй список задач в карте и ссылку Part of #<map> в каждой задаче.
- Тип дочерней задачи: wayfinder:research, wayfinder:prototype,
  wayfinder:grilling или wayfinder:task.
- Блокировки оформляй нативными зависимостями GitHub; если они недоступны,
  строкой Blocked by: #<number>. Задача доступна после закрытия блокеров.
- Следующая задача — первая открытая задача карты без блокеров и исполнителя.
  При начале назначь исполнителя; при завершении запиши результат,
  закрой задачу и добавь ссылку с выводом в Decisions-so-far карты.
