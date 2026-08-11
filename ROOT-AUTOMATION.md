# Полностью автоматическая запись на Redmi Note 8 Pro

## Текущий статус устройства

Проверено через ADB:

- `su`: отсутствует;
- bootloader: `locked`;
- verified boot: `green`;
- `ro.oem_unlock_supported=1`;
- `sys.oem_unlock_allowed=0`.

Вывод: на текущем состоянии телефона полностью автоматическая запись обеих сторон обычным APK невозможна. Нужен привилегированный доступ: root/Magisk или системная прошивка/модуль.

## Что уже подготовлено

- APK теперь запрашивает privileged permissions и при их наличии автоматически использует `MediaRecorder.AudioSource.VOICE_CALL`.
- Если privileged permissions не выданы, обычный APK продолжает работать в диагностическом/импортном режиме.
- Собран Magisk-модуль:
  - `diagnostics/artifacts/mo54-calls-privapp-magisk.zip`;
  - кладёт release APK в `/system/priv-app/MO54Calls/MO54Calls.apk`;
  - добавляет `privapp-permissions-ru.mo54.calls.xml`;
  - добавляет `default-permissions-ru.mo54.calls.xml`.

## Безопасный порядок перехода к автоматическому режиму

1. Сделать резервную копию данных телефона.
2. Включить OEM unlocking в настройках разработчика, если доступно.
3. Разблокировать загрузчик официальным инструментом Xiaomi.
   - Это обычно стирает данные телефона.
   - Этот шаг нельзя выполнять без отдельного подтверждения владельца.
4. Получить root через Magisk.
5. Установить `diagnostics/artifacts/mo54-calls-privapp-magisk.zip` в Magisk.
6. Перезагрузить телефон.
7. Удалить или отключить debug APK `ru.mo54.calls.debug`, чтобы не было двух PHONE_STATE-приёмников.
8. Проверить:
   - `adb shell su -c id`;
   - `adb shell dumpsys package ru.mo54.calls | grep CAPTURE_AUDIO_OUTPUT`;
   - в MO54 Calls → «Диагностика»: `Privileged call-audio: да`.
9. Выполнить приёмку:
   - 3 входящих подряд;
   - 3 исходящих подряд;
   - заблокированный экран;
   - после перезагрузки;
   - отсутствие сети / retry sync;
   - `work` синхронизируется, `not_work_related` удаляется.

## Важное ограничение

Magisk-модуль подготовлен, но его работоспособность на конкретной MIUI-сборке можно подтвердить только после root-установки и теста. Если MIUI блокирует `VOICE_CALL` даже для priv-app, следующий шаг — системный модуль, вызывающий MediaTek CallRecorderService, либо совместимая прошивка.
