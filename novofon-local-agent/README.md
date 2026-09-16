# MO54 Calls — локальный агент Novofon

Этот пакет запускается только на домашнем Windows-ПК. Он работает в отдельном
профиле Microsoft Edge, скачивает запись штатной кнопкой личного кабинета,
расшифровывает её локально и передаёт в MO54 Calls только текст.

Никогда не сохраняйте пароль Novofon, ссылку на запись, текст звонка, токены
или аудио в репозитории. Аудио и профиль браузера находятся только в
`%LOCALAPPDATA%\MO54CallsAgent`.

## Установка на домашнем ПК

В PowerShell из каталога этого пакета:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1 -CrmUrl https://calls.example.invalid
```

Для первой безопасной проверки кабинета без ASR/PyTorch, моделей, Планировщика
и скачивания аудио используйте:

```powershell
.\setup.ps1 -CrmUrl https://calls.example.invalid -BrowserOnly -SkipScheduledTask -SkipFfmpegInstall
```

Скрипт создаёт venv, ставит пакет, `ffmpeg` и браузер Playwright, а также
задачу Планировщика. Затем выполните:

```powershell
& "$env:LOCALAPPDATA\MO54CallsAgent\venv\Scripts\mo54-agent.exe" preflight
& "$env:LOCALAPPDATA\MO54CallsAgent\venv\Scripts\mo54-agent.exe" provision-browser
```

В открывшемся Edge вручную войдите в Novofon. URL списка и CSS-селекторы
указываются только в `%LOCALAPPDATA%\MO54CallsAgent\config.json`; шаблон
создаётся автоматически. Агент не начнёт скачивание, пока там нет точного
`call_session_id` и селектора штатной кнопки сохранения.

Для моделей нужно один раз принять условия моделей GigaAM и pyannote в
Hugging Face, после чего выполнить `preflight --prepare-models`. Токен
запрашивается интерактивно и не сохраняется. `enroll-manager PATH` создаёт
DPAPI-защищённый локальный голосовой эталон менеджера.

Токен CRM вводится интерактивно:

```powershell
mo54-agent set-crm-token
```

Перед этим MO54 Calls разворачивается отдельным штатным обновлением: в его
защищённом серверном `.api.secrets.env` задаётся новый `LOCAL_AGENT_TOKEN`,
совпадающий с введённым в Credential Manager, и применяется миграция `015`.
Для отзыва агента удалите или замените этот токен и пересоздайте только
API-контейнер с обновлённым окружением. Сам агент не изменяет `/opt/mo54`, `mo54.service` или
контейнеры MO54; порядок выпуска описан в `docs/TRANSCRIPTION_AI_ROLLOUT.md`.

## Команды

`preflight`, `provision-browser`, `inspect-layout`, `inventory`, `enroll-manager PATH`,
`download-pilot CALL_SESSION_ID`, `transcribe-pilot CALL_SESSION_ID`,
`assign-pilot-roles CALL_SESSION_ID --manager-label "Спикер 1"`,
`deliver-pilot CALL_SESSION_ID`, `run-once`, `status`, `install-task`,
`set-crm-token`.

Для приёмки одного звонка сначала вручную войдите в выделенный профиль Edge,
запустите `inspect-layout` и `inventory`, а затем выберите один свежий
`CALL_SESSION_ID` из локальной инвентаризации. `download-pilot` скачивает
только этот идентификатор и останавливается: он не запускает ASR, CRM или
обработку остальных звонков. `transcribe-pilot` выводит только контрольные
метаданные (хэш, длительность, роли и время); качество текста и роли нужно
подтвердить локально до явного `deliver-pilot`.

Если автоматическое сопоставление голоса оставило роль `unknown`, оператор может
после локального просмотра транскрипта подтвердить конкретную метку спикера.
`assign-pilot-roles` меняет роль только в локальном файле транскрипта и добавляет
аудиторскую метку ручного подтверждения; CRM и внешние сервисы эта команда не
открывает.

`run-once` удерживает Windows от сна только пока идёт скачивание или ASR. При
заблокированном экране и любой проблеме UI он фиксирует безопасный код ошибки
и откладывает попытку: обход блокировки Windows не используется.
