# MoscowFlatFinder — Claude Code

При настройке пользовательского поиска полностью следуй
`docs/agent-onboarding.md`. Задавай по одному вопросу и сохраняй личные данные
только в `~/Library/Application Support/MoscowFlatFinder`, вне Git.

Не запускай полный сбор, массовый Vision refresh или расписание без явного
подтверждения пользователя. Не обходи CAPTCHA/2FA и не придумывай исполняемые
hard constraints, которых нет в `docs/configuration.md`.

При разработке не добавляй test framework или постоянные test-файлы. Используй
inline smoke и команды проверки из `README.md`; временные fixtures не коммить.
