# Статус реализации

## Реализовано

- Android-проект Kotlin/Compose, minSdk 29, package `ru.mo54.calls`.
- Room-модель звонков и устойчивой очереди.
- Временная фиксация всех вызовов до классификации.
- Транзакционно-восстанавливаемое удаление нерабочего звонка и аудио.
- PHONE_STATE detector, сверка и восстановление из Call Log.
- Foreground MediaRecorder с диагностическим выбором источника.
- Импорт системных/MIUI-записей через MediaStore в private storage.
- Share-import из Google Phone: MO54 зарегистрирован как `ACTION_SEND` audio-target, копирует переданный WAV/M4A в private storage, привязывает к последнему Call Log и считает SHA-256.
- Privileged mode: APK запрашивает системные call-audio permissions и при их наличии автоматически использует `VOICE_CALL` вместо публичных источников.
- Magisk-модуль `diagnostics/artifacts/mo54-calls-privapp-magisk.zip`: установка release APK как priv-app, privapp allowlist и default runtime permissions.
- SHA-256 аудио и последовательность metadata → audio → outcome.
- WorkManager retry, backoff, ручной повтор и естественные idempotency keys.
- Device token в Android Keystore, только HTTPS, логи без body/headers.
- Экраны подключения, permissions, классификации, истории и диагностики.
- OpenAPI 3.1 и contract-тесты.

## Проверено локально

- `testDebugUnitTest`: 8 тестов, 0 failures.
- `lintDebug`: 0 errors.
- `assembleDebug`: успешно.
- `assembleRelease`: успешно; acceptance build подписан Android debug key.
- Оба APK проходят `apksigner verify` по APK Signature Scheme v2.

## Блокирующие внешние ворота

### GO/NO-GO записи

Redmi Note 8 Pro подключен и проверен. Обычный APK MediaRecorder на публичных
источниках (`VOICE_RECOGNITION`, `MIC`, `VOICE_COMMUNICATION`) создает файлы,
но во время SIM-звонка в них тишина — это `NO-GO` для полностью автоматической
записи силами обычного APK.

Рабочий не-root обход найден: Google Phone как default dialer вручную пишет обе
стороны через системный Telecom (`inputSource 4`), проигрывает автофразу и дает
кнопку «Поделиться». Обновленный MO54 импортирует этот файл через share sheet;
проверенный артефакт:
`diagnostics/artifacts/google-phone-shared-outgoing.wav`, 11.28 с / 180524 байт,
SHA-256 `1a8fd4e493bfa5187dc43b35d1fbe0aa9083eba71207c0b76da5b4e21829cc54`.

До полного `GO` по исходному v1 нужно повторить Google Phone + share-import для
3 входящих и 3 исходящих подряд и пройти проверки блокировки/перезагрузки/сети.
Если требуется именно автоматическая запись без ручного нажатия и share, дальше
остается лестница: root/Magisk priv-app module → системный модуль MediaTek
CallRecorderService → совместимая ROM/другое устройство. Подготовлен первый
вариант этой лестницы: `diagnostics/artifacts/mo54-calls-privapp-magisk.zip`.

Текущий телефон: `su` отсутствует, bootloader `locked`, verified boot `green`,
`sys.oem_unlock_allowed=0`. Поэтому модуль нельзя установить прямо сейчас без
разблокировки/root. Перед root или ROM обязательны резервная копия и отдельное
подтверждение владельца.

### Backend

Репозиторий существующего backend MO54 не предоставлен, поэтому его код и
миграции физически не могли быть изменены. Готовы OpenAPI и инструкция
`BACKEND-INTEGRATION.md`; после подключения репозитория endpoints должны быть
реализованы и проверены на staging.
