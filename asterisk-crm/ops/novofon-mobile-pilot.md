# Пилот MO54 Calls: Novofon + Redmi без SIP на телефоне

Этот документ — рабочая последовательность включения мобильного контура v1.
Он не меняет `/opt/mo54`, `mo54.service`, его Docker-контейнеры или его nginx
virtual host.

Целевой путь:

```text
Входящий: клиент → +7 (383) 235-92-77 в Novofon → Redmi по мобильной связи
Исходящий: CRM в браузере Redmi → Call API Novofon → звонок на Redmi → клиент
События: Novofon → HTTPS webhook CRM → карточка звонка / клиента / задача
```

Не используйте для продаж обычную системную звонилку Redmi: такой звонок не
создаёт управляемый callback intent в CRM. Рабочий исходящий в пилоте начинается
кнопкой **«Позвонить»** в CRM.

## 0. Условия, без которых не переключаем номер

В кабинете Novofon должны быть подтверждены все пункты:

- доступ к **Call API** (callback `start.employee_call`);
- доступ к **Data API** (`get.calls_report` для сверки);
- возможность настроить HTTP-уведомления;
- городской номер `+7 (383) 235-92-77` активен и доступен как АОН исходящего;
- Redmi закреплён за сотрудником Novofon как обычный мобильный номер для
  переадресации/callback;
- запись разговора в Novofon включена и её срок хранения согласован.

Если Call API недоступен на тарифе, номер не переводится на этот боевой путь:
CRM не сможет надежно запускать исходящие.

В пилоте не подключаем платную речевую аналитику. Без выданного Novofon
транскрипта CRM не запускает GigaChat и не показывает вымышленные резюме,
оценки или рекомендации.

## 1. DNS: создать отдельное имя CRM

В DNS-зоне `мягкиеокна54.рф` создайте ровно одну A-запись:

| Поле | Значение |
| --- | --- |
| Имя/host | `calls` |
| Тип | `A` |
| Значение | `72.56.36.46` |
| TTL | стандартный провайдера (например, 300–3600 секунд) |

В браузере адрес будет `https://calls.мягкиеокна54.рф`. На VPS и в конфигурации
nginx используется его Punycode-эквивалент:

```text
calls.xn--54-6kclkrncnpj3r.xn--p1ai
```

Не меняйте A-запись `app.мягкиеокна54.рф` и не направляйте `calls` на IP
контейнера: оба действия нарушают границу с существующим MO54.

Проверка после распространения DNS на VPS:

```bash
getent ahostsv4 calls.xn--54-6kclkrncnpj3r.xn--p1ai
```

В выводе должен быть `72.56.36.46`.

## 2. Заполнить серверное окружение

На VPS есть один обычный файл конфигурации и два закрытых файла секретов:

- `/opt/asterisk-crm/.env` — общая конфигурация, включая уже существующие инфраструктурные секреты; новые секреты Novofon в нём не хранятся;
- `/opt/asterisk-crm/.api.secrets.env` — только Call API и два webhook-секрета;
- `/opt/asterisk-crm/.worker.secrets.env` — только токен Data API.

Не копируйте их на телефон и не присылайте содержимое в чат. Asterisk не
получает ни один из файлов Novofon-секретов.

```bash
sudo chmod 600 /opt/asterisk-crm/.env
sudoedit /opt/asterisk-crm/.env
```

Добавьте/проверьте значения из `.env.example`:

```dotenv
PUBLIC_ORIGIN=https://calls.xn--54-6kclkrncnpj3r.xn--p1ai
SESSION_COOKIE_NAME=mo54_calls_session
SESSION_COOKIE_SECURE=true
SESSION_COOKIE_SAMESITE=strict
CRM_LOGIN_MAX_ATTEMPTS=5
CRM_LOGIN_WINDOW_SECONDS=900

NOVOFON_WEBHOOK_ALLOWED_IPS=37.139.38.215
NOVOFON_CALL_API_URL=https://callapi-jsonrpc.novofon.ru/v4.0
NOVOFON_DATA_API_URL=https://dataapi-jsonrpc.novofon.ru/v2.0
NOVOFON_VIRTUAL_NUMBER=73832359277
NOVOFON_ACCOUNT_TIMEZONE=Asia/Novosibirsk
NOVOFON_RECONCILE_ENABLED=false
NOVOFON_RECONCILE_INTERVAL_SECONDS=3600
NOVOFON_TRANSCRIPT_ENABLED=false
```

Создайте закрытые файлы из шаблонов и сразу ограничьте права:

```bash
cd /opt/asterisk-crm
cp .api.secrets.env.example .api.secrets.env
cp .worker.secrets.env.example .worker.secrets.env
chmod 600 .api.secrets.env .worker.secrets.env
sudoedit .api.secrets.env
sudoedit .worker.secrets.env
```

В `.api.secrets.env` внесите Call API token и два разных значения из
`openssl rand -hex 32`: первое — `NOVOFON_WEBHOOK_PATH_SECRET`, второе —
`NOVOFON_WEBHOOK_SECRET`. Первое используется только в URL, второе — только
как `integration_key` в JSON уведомления. Они не взаимозаменяемы.

В `.worker.secrets.env` внесите Data API token. Если Novofon пока выдаёт только
один общий token, временно внесите его в оба закрытых файла; не переносите его
в `.env`. Перед выпуском token в Novofon: **Настройки → Безопасность → Правила безопасности API**
(название может немного отличаться) добавьте ровно адрес `72.56.36.46/32`.
Не добавляйте `0.0.0.0/0`, не добавляйте IP мобильного телефона и не открывайте
SIP/RTP ради API.

После обновления кода и конфигурации применяйте миграции и контейнеры штатной командой
проекта:

```bash
cd /opt/asterisk-crm
docker compose -f docker-compose.yml -f docker-compose.vps.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.vps.yml run --rm migrate
docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d api worker
```

## 3. Опубликовать только CRM по HTTPS

Шаблоны nginx находятся в `ops/nginx/`. Скрипт создает **новый** site
`mo54-calls-calls.xn--54-6kclkrncnpj3r.xn--p1ai.conf`; перед любой записью он
проверяет DNS и отказывается перезаписывать чужой site. Он не меняет
конфигурацию MO54.

На VPS один раз:

```bash
cd /opt/asterisk-crm
sudo chmod +x ops/prepare-calls-ingress.sh ops/check-calls-ingress.sh
sudo CRM_VPS_PUBLIC_IP=72.56.36.46 ./ops/prepare-calls-ingress.sh --prepare
```

Команда подготовит только HTTP-ответ для ACME и напечатает команду Certbot.
Подставьте свой реальный технический email:

```bash
sudo certbot certonly --webroot -w /var/www/letsencrypt \
  -d calls.xn--54-6kclkrncnpj3r.xn--p1ai \
  --email you@example.com --agree-tos --no-eff-email
sudo CRM_VPS_PUBLIC_IP=72.56.36.46 ./ops/prepare-calls-ingress.sh --enable-https
sudo ./ops/check-calls-ingress.sh
```

Критерий: `https://calls.мягкиеокна54.рф/health` возвращает `2xx`,
`https://app.мягкиеокна54.рф` остаётся доступным. API-порт `8080` не открывается
в firewall и не публикуется Docker напрямую.

## 4. Настроить Novofon

Названия разделов в личном кабинете могут незначительно отличаться, но нужны
следующие сущности.

### Сотрудник и входящий сценарий

1. Создайте или откройте сотрудника Novofon, соответствующего пользователю CRM.
2. Укажите номер Redmi в международном виде `79…` и проверьте обычный мобильный
   звонок на телефон.
3. Для `+7 (383) 235-92-77` создайте входящий сценарий: **переадресация на
   мобильный сотрудника Redmi**. Это обычная GSM/VoLTE-переадресация, не SIP и
   не WireGuard.
4. Включите запись разговора в настройках сценария/номера. Не включайте речевую
   аналитику, пока не принято отдельное решение по её цене и правовым условиям.
5. Для исходящих назначьте этот городской номер как АОН сотрудника.

### HTTP-уведомления

Создайте уведомления о событиях:

- завершение звонка (`CALL_END`);
- появление записанного разговора (`RECORD_CALL`).

URL должен быть именно таким (секрет в угловых скобках заменяется значением
переменной, сами скобки не вводятся):

```text
https://calls.xn--54-6kclkrncnpj3r.xn--p1ai/api/integrations/novofon/<NOVOFON_WEBHOOK_PATH_SECRET>/events
```

В теле уведомления/настройке интеграционного ключа задайте
`integration_key`, равный `NOVOFON_WEBHOOK_SECRET`. Если интерфейс позволяет
фильтр, ограничьте уведомления виртуальным номером `73832359277`.

Не оставляйте тело пустым и не меняйте имена полей. В Novofon создайте **два
отдельных HTTP-уведомления**, методом `POST`, со следующими телами. Перед
сохранением замените только `<NOVOFON_WEBHOOK_SECRET>` на секрет, созданный на
сервере; угловые скобки не вводятся. Шаблоны соответствуют полям, которые
Novofon публикует для этих уведомлений.

**Завершение звонка (`CALL_END`):**

```json
{
  "integration_key": "<NOVOFON_WEBHOOK_SECRET>",
  "notification_name": {{notification_name}},
  "virtual_phone_number": {{virtual_phone_number}},
  "notification_time": {{notification_time}},
  "external_id": {{external_id}},
  "call_session_id": {{call_session_id}},
  "contact_info": {
    "contact_phone_number": {{contact_phone_number}},
    "communication_number": {{communication_number}}
  },
  "employee_info": {
    "employee_full_name": {{employee_full_name}},
    "employee_id": {{employee_id}},
    "extension_phone_number": {{extension_phone_number}}
  },
  "call_info": {
    "call_source": {{call_source}},
    "scenario_name": {{scenario_name}},
    "talk_time_duration": {{talk_time_duration}},
    "total_time_duration": {{total_time_duration}},
    "wait_time_duration": {{wait_time_duration}},
    "tag_names": {{tag_names}},
    "call_status": {{call_status}}
  },
  "direction": {{direction}},
  "event": "CALL_END"
}
```

**Записанный разговор (`RECORD_CALL`):**

```json
{
  "integration_key": "<NOVOFON_WEBHOOK_SECRET>",
  "notification_name": {{notification_name}},
  "virtual_phone_number": {{virtual_phone_number}},
  "notification_time": {{notification_time}},
  "scenario_name": {{scenario_name}},
  "contact_info": {
    "contact_phone_number": {{contact_phone_number}},
    "communication_number": {{communication_number}},
    "contact_id": {{contact_id}},
    "contact_full_name": {{contact_full_name}}
  },
  "call_session_id": {{call_session_id}},
  "employee_info": {
    "employee_full_name": {{employee_full_name}},
    "employee_id": {{employee_id}},
    "extension_phone_number": {{extension_phone_number}}
  },
  "call_record_file_info": {
    "file_link": {{file_link}},
    "call_record_duration": {{file_duration}}
  },
  "tag_ids": {{tag_ids}},
  "tag_names": {{tag_names}},
  "event": "RECORD_CALL"
}
```

После сохранения оба уведомления должны быть проверены одним реальным тестовым
звонком до переноса рабочего номера на мобильный сценарий: в CRM должен
появиться ровно один звонок и, после подготовки записи, одна кнопка
«Открыть запись в Novofon».

Не тестируйте webhook `curl`-командой с VPS: nginx правильно отклонит её,
потому что источник не `37.139.38.215`. Тест выполняется только реальным
событием Novofon.

### Call API и Data API

1. В правилах API оставьте `72.56.36.46/32`.
2. Выдайте access token: Call API — только в `.api.secrets.env`, Data API —
   только в `.worker.secrets.env`.
3. В CRM привяжите пользователя-менеджера к employee ID Novofon и мобильному
   номеру Redmi. Без этой связи кнопка callback должна отказывать безопасно, а
   не звонить на произвольный телефон.
4. Включите `NOVOFON_RECONCILE_ENABLED=true` только после тестового успешного
   ответа Data API. Сверка берёт 24 часа с перекрытием, поэтому повторные
   записи не должны появляться.

## 5. Порядок пилота

Сначала сделайте ровно два контрольных разговора с тестовыми номерами:

1. **Входящий:** внешний телефон → `+7 (383) 235-92-77` → Redmi. После
   завершения проверяются одна карточка звонка, номер/контакт, направление,
   длительность, ответственный, metadata записи и при необходимости задача.
2. **Исходящий:** на Redmi открыть CRM в браузере → карточка тестового контакта
   → «Позвонить» → принять callback Novofon на Redmi → разговор с тестовым
   номером. Убедиться, что быстрый второй тап не создал второй вызов.

В карточке записи нажмите «Открыть запись в Novofon». Обычный API-ответ и
неавторизованный пользователь не должны видеть исходную provider URL.

Только после двух успешных проверок сделайте 10 входящих и 10 исходящих подряд.
Для каждого сверяются: одна CRM-карточка на `call_session_id`, контакт по
нормализованному телефону, направление, длительность, ответственный, задача
для пропущенного звонка и доступ к записи в кабинете Novofon.

## 6. Эксплуатация и откат

- Номер не переключается окончательно до прохождения 20 тестов.
- Записи остаются у Novofon; CRM хранит идентификаторы и защищенный переход.
  Установите срок хранения у провайдера и текст уведомления о записи до первой
  реальной продажи.
- При сбое CRM входящий сценарий Novofon продолжает доставлять вызов на Redmi.
  События будут догнаны Data API после восстановления.
- При сбое Call API не делайте исходящий из обычной звонилки «для обхода»:
  фиксируйте отказ и используйте резервный согласованный процесс, иначе CRM
  потеряет управляемую связь звонка с клиентом.
- Asterisk/WireGuard не удаляются, но их обслуживание не является условием
  работоспособности пилота.

Проверки после обновления выполняются так:

```bash
cd /opt/asterisk-crm
sudo ./ops/check-calls-ingress.sh
./ops/healthcheck.sh
```
