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
`review-pilot CALL_SESSION_ID`, `review-uri URI`, `deliver-pilot CALL_SESSION_ID`, `run-once`, `status`, `install-task`,
`install-review-uri`,
`set-crm-token`.

## One-click review from a MO54 Calls card

After running `setup.ps1`, Windows registers the per-user URI handler
`mo54-calls-review://`. A button in a Novofon call card can open a URI in the
following exact form:

```text
mo54-calls-review://review/CALL_SESSION_ID
```

The card passes only `CALL_SESSION_ID`; it does not pass audio, a Novofon URL,
cookies, passwords, or a CRM token. The local agent validates the URI, finds
the exact call only in its local SQLite store, and opens the loopback editor
only when the call is already locally `transcribed`. It never starts a
download, ASR, browser session, or CRM request from the link itself.

When the local review is approved, Windows explicitly asks whether to send the
approved text to MO54 Calls. Choosing **No** keeps the sealed review only on
this PC; the next click validates that same review and offers delivery again.
Choosing **Yes** uses the same verified path as `deliver-pilot`: only approved
text, roles, hashes, and metadata are sent. Audio and browser data stay local.

If the browser says that no application can open the link, run only
`install-review-uri` for the current Windows user and then click the card
button again. If the agent reports that the call is not ready, prepare that
exact call locally first; the deep link deliberately does not download or
transcribe it on its own.

For an existing installation, update only this Windows association with:

```powershell
& "$env:LOCALAPPDATA\MO54CallsAgent\venv\Scripts\mo54-agent.exe" install-review-uri
```

This command writes only the current user's `HKCU` protocol handler. It does
not install, enable, or run `MO54CallsAgent`; the 30-minute task remains under
its existing explicit controls.

Для приёмки одного звонка сначала вручную войдите в выделенный профиль Edge,
запустите `inspect-layout` и `inventory`, а затем выберите один свежий
`CALL_SESSION_ID` из локальной инвентаризации. `download-pilot` скачивает
только этот идентификатор и останавливается: он не запускает ASR, CRM или
обработку остальных звонков. `transcribe-pilot` выводит только контрольные
метаданные (хэш, длительность, роли и время); качество текста и роли нужно
подтвердить локально через явный `review-pilot CALL_SESSION_ID` до
`deliver-pilot`.

Если автоматическое сопоставление голоса оставило роль `unknown`, оператор может
после локального просмотра транскрипта подтвердить конкретную метку спикера.
`assign-pilot-roles` меняет роль только в локальном файле транскрипта и добавляет
аудиторскую метку ручного подтверждения; CRM и внешние сервисы эта команда не
открывает.

`review-pilot` открывает короткоживущий редактор только на `127.0.0.1`. В нём
выберите, кто сказал первую фразу, и правьте обычный текст без тегов. Когда
начинает говорить другой человек, поставьте курсор перед его первой фразой и
нажмите `Enter`: редактор создаст новую реплику и переключит роль. Плеер нужен
только для прослушивания спорного места — ставить его на границу реплики не
нужно. `Shift+Enter` оставляет перенос внутри реплики, а `Backspace` в начале
новой реплики отменяет ошибочное разделение. В этом режиме система не
придумывает временные отметки: каждая реплика передаётся как «время не
размечено», но сохраняет точные текст, роль и порядковый номер для evidence.
Исходный ASR-транскрипт остаётся отдельным локальным эталоном; утверждённая
версия и WER/CER/метрики ролей сохраняются
только в `%LOCALAPPDATA%\MO54CallsAgent\transcripts\reviews`. Файл review
запечатывается отдельным ключом из Windows Credential Manager; ключ не
отправляется в CRM и не лежит рядом с аудио.

В текущем пилоте даже `run-once` не передаст неутверждённый текст в CRM: это
не настраивается флагом. `deliver-pilot` принимает только утверждённую
review-версию, проверенную по хэшам исходного ASR и самой review-версии. После подтверждённой доставки повторная команда
сообщает об уже доставленном звонке и не создаёт вторую скрытую версию.

`run-once` удерживает Windows от сна только пока идёт скачивание или ASR. При
заблокированном экране и любой проблеме UI он фиксирует безопасный код ошибки
и откладывает попытку: обход блокировки Windows не используется.
