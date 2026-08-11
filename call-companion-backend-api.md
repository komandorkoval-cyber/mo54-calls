# MO54 Calls: backend API для Android companion

Статус: `спецификация API для разработки`  
Дата: `2026-06-23`

## 1. Назначение

API нужен, чтобы Android-приложение `MO54 Calls` передавало в MO54:

- информацию о звонках;
- аудиозаписи;
- итоги разговоров;
- следующие действия;
- диагностические данные устройства.

API является минимальным слоем до полноценного CRM. В будущем CRM должна читать эти данные и связывать их со сделками.

## 2. Базовые правила

- Все endpoints находятся под `/api/call-companion`.
- Формат JSON: UTF-8.
- Время передается в ISO 8601.
- Все операции должны быть идемпотентными.
- Для первого этапа основной пользователь: `worker-igor`.
- Backend не должен зависеть от Google Sheets.
- Аудиофайлы хранятся в backend storage, не в Google Sheets.

## 3. Авторизация устройства

### 3.1. Привязка устройства

`POST /api/call-companion/device-link`

Request:

```json
{
  "pairingCode": "123456",
  "deviceName": "Igor Android",
  "androidVersion": "14",
  "manufacturer": "Samsung",
  "model": "SM-XXXX",
  "appVersion": "1.0.0"
}
```

Response:

```json
{
  "deviceId": "call-device-uuid",
  "workerId": "worker-igor",
  "workerName": "Игорь",
  "deviceToken": "secret-token",
  "expiresAt": null
}
```

Правила:

- `pairingCode` создает администратор или backend-разработчик.
- `deviceToken` используется Android-приложением для следующих запросов.
- Token передается в заголовке:

```text
Authorization: Bearer <deviceToken>
```

### 3.2. Получение конфигурации

`GET /api/call-companion/config`

Response:

```json
{
  "workerId": "worker-igor",
  "requiredOutcomeForWorkCalls": true,
  "recordingNoticeText": "Разговор записывается для контроля качества обслуживания.",
  "maxAudioSizeMb": 100,
  "allowedAudioMimeTypes": ["audio/mp4", "audio/mpeg", "audio/aac", "audio/wav"],
  "nextActionTypes": ["call", "message", "prepare_proposal", "schedule_measurement", "send_contract", "check_payment", "other"]
}
```

## 4. Звонки

### 4.1. Создать или обновить звонок

`POST /api/call-companion/calls`

Request:

```json
{
  "localCallId": "android-local-uuid",
  "deviceId": "call-device-uuid",
  "workerId": "worker-igor",
  "direction": "incoming",
  "phoneRaw": "+7 913 000-00-00",
  "phoneNormalized": "+79130000000",
  "startedAt": "2026-06-23T09:10:00.000+07:00",
  "endedAt": "2026-06-23T09:14:30.000+07:00",
  "durationSec": 270,
  "recordingStatus": "recorded",
  "noticeStatus": "played_to_call",
  "source": "android-companion",
  "deviceSnapshot": {
    "androidVersion": "14",
    "manufacturer": "Samsung",
    "model": "SM-XXXX",
    "appVersion": "1.0.0"
  }
}
```

Response:

```json
{
  "callId": "call-server-uuid",
  "localCallId": "android-local-uuid",
  "syncStatus": "accepted",
  "updatedAt": "2026-06-23T02:14:35.000Z"
}
```

Idempotency:

- уникальный ключ: `deviceId + localCallId`;
- повторный запрос обновляет запись, а не создает дубль;
- backend не должен перетирать заполненный итог пустыми значениями.
- `phoneNormalized` может быть `null`, если ОС не предоставила распознаваемый номер;
- Android отправляет только звонки, подтвержденные пользователем как рабочие;
  `not_work_related` является локальной командой удаления и в API не передается.

## 5. Аудио

### 5.1. Загрузить аудио

`POST /api/call-companion/calls/:callId/audio`

Content-Type: `multipart/form-data`

Fields:

- `localCallId`;
- `audioSha256`;
- `recordingStatus`;
- `mimeType`;
- `durationSec`;
- `file`.

Response:

```json
{
  "audioAssetId": "audio-uuid",
  "callId": "call-server-uuid",
  "recordingStatus": "recorded",
  "receivedSha256": "hex-sha256",
  "sizeBytes": 1234567,
  "createdAt": "2026-06-23T02:15:00.000Z"
}
```

Правила:

- если `audioSha256` уже загружен для этого звонка, backend возвращает существующий `audioAssetId`;
- если checksum не совпадает, возвращается ошибка `audio_checksum_mismatch`;
- если размер превышает лимит, возвращается ошибка `audio_too_large`;
- аудио не обязательно для звонков со статусом `unsupported`, `permission_denied`, `failed`, `not_attempted`.

## 6. Итог звонка

### 6.1. Сохранить итог

`PATCH /api/call-companion/calls/:callId/outcome`

Request:

```json
{
  "outcomeStatus": "continue_work",
  "summary": "Клиент хочет мягкие окна на веранду, просит расчет двух вариантов.",
  "objection": "Сравнивает цену с другим исполнителем.",
  "nextActionType": "prepare_proposal",
  "nextActionAt": "2026-06-24T10:00:00.000+07:00",
  "nextActionComment": "Подготовить КП с монтажом и без монтажа.",
  "noticeConfirmedByWorker": true
}
```

Response:

```json
{
  "callId": "call-server-uuid",
  "outcomeSaved": true,
  "followupTaskId": "call-followup-uuid",
  "updatedAt": "2026-06-23T02:16:00.000Z"
}
```

Правила:

- для активных результатов следующий шаг обязателен;
- для `not_work_related` следующий шаг не нужен;
- для `no_answer` поле `summary` может быть пустым;
- backend создает или обновляет `call_followup_task`;
- повторный запрос не создает дубль задачи.

## 7. Диагностика устройства

### 7.1. Отправить результат диагностики

`POST /api/call-companion/diagnostics`

Request:

```json
{
  "deviceId": "call-device-uuid",
  "workerId": "worker-igor",
  "androidVersion": "14",
  "manufacturer": "Samsung",
  "model": "SM-XXXX",
  "appVersion": "1.0.0",
  "incomingCallDetected": true,
  "outgoingCallDetected": true,
  "incomingAudioRecorded": true,
  "outgoingAudioRecorded": true,
  "bothSidesAudibleIncoming": true,
  "bothSidesAudibleOutgoing": true,
  "noticePlayedToCall": false,
  "worksOnLockedScreen": true,
  "worksAfterReboot": true,
  "conclusion": "partial",
  "notes": "Запись работает, автофраза в линию не подтверждена."
}
```

Response:

```json
{
  "diagnosticId": "diagnostic-uuid",
  "saved": true
}
```

Допустимые `conclusion`:

- `pass`;
- `partial`;
- `fail`;
- `blocked_by_os`;
- `needs_device_change`.

## 8. Список звонков

`GET /api/call-companion/calls?workerId=worker-igor&status=pending`

Response:

```json
{
  "items": [
    {
      "callId": "call-server-uuid",
      "workerId": "worker-igor",
      "direction": "incoming",
      "phoneNormalized": "+79130000000",
      "startedAt": "2026-06-23T09:10:00.000+07:00",
      "durationSec": 270,
      "recordingStatus": "recorded",
      "noticeStatus": "played_to_call",
      "outcomeStatus": "continue_work",
      "hasAudio": true,
      "hasFollowupTask": true
    }
  ]
}
```

## 9. Ошибки

Формат ошибки:

```json
{
  "error": "audio_too_large",
  "message": "Audio file exceeds maxAudioSizeMb.",
  "retryable": false
}
```

Коды:

- `unauthorized_device`;
- `device_not_linked`;
- `invalid_payload`;
- `call_not_found`;
- `audio_too_large`;
- `audio_checksum_mismatch`;
- `unsupported_mime_type`;
- `outcome_requires_next_action`;
- `temporary_storage_error`;
- `internal_error`.

## 10. Будущая интеграция с CRM

Backend должен хранить звонки так, чтобы позже CRM могла:

- найти клиента по `phoneNormalized`;
- создать лид из звонка;
- связать звонок со сделкой;
- показать историю коммуникаций;
- создать задачу следующего шага;
- использовать аудио и итог для обучения.

В первом этапе CRM-сущности не создаются автоматически.
