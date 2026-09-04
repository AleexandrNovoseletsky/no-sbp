# NoSBP

Сервис генерации QR-кодов для оплаты по банковским реквизитам согласно
ГОСТ Р 56042-2014. Код отдаётся обычной ссылкой на изображение, поэтому
встраивается в любой шаблон письма или счёта.

Плательщик сканирует код банковским приложением и переводит деньги на
расчётный счёт получателя. Эквайринг не используется, комиссия за приём
платежа не взимается. Зачисление происходит в сроки обычного банковского
перевода — как правило, на следующий рабочий день.

## Содержание

- [Как это работает](#как-это-работает)
- [Тарификация](#тарификация)
- [Персональные данные](#персональные-данные)
- [Развёртывание](#развёртывание)
- [Эксплуатация](#эксплуатация)
- [Модель доступа](#модель-доступа)
- [Разработка](#разработка)
- [Устройство проекта](#устройство-проекта)
- [Лицензия](#лицензия)

## Как это работает

Заказчик регистрируется, вводит реквизиты организации и получает ключ API.
Дальше в шаблон письма или счёта добавляется одна строка:

```html
<img src="https://example.com/v1/qr?token=КЛЮЧ&sum=4700000&purpose=Заказ+1234"
     alt="QR-код для оплаты">
```

Ключ передаётся в параметре запроса намеренно: шаблонизаторы CRM
(RetailCRM, МойСклад и другие) поддерживают только подстановку адреса
изображения и не умеют отправлять запросы с заголовками. Ограничения
такой схемы описаны в разделе [Модель доступа](#модель-доступа).

Для интеграций, выполняющих полноценные HTTP-запросы, предусмотрен
`POST /v1/qr` с ключом в заголовке `Authorization`.

### Параметры запроса

| Параметр | Обязательный | Описание |
|---|---|---|
| `token` | да | Ключ API |
| `org` | нет | Алиас организации-получателя; по умолчанию — основная |
| `sum` | нет | Сумма в копейках. Без неё плательщик вводит сумму сам |
| `purpose` | нет | Назначение платежа, до 210 символов |
| `last_name`, `first_name`, `middle_name` | нет | ФИО плательщика |
| `phone` | нет | Телефон плательщика |
| `on_error` | нет | `image` (по умолчанию) или `status` |

### Обработка ошибок

QR-код находится в теге `<img>` уже отправленного письма. Код ответа 4xx
получатель увидел бы как значок сломанного изображения, поэтому на
GET-запросах ошибка по умолчанию возвращается изображением с текстом,
а машиночитаемый код передаётся в заголовке `X-NoSBP-Error`. Параметр
`on_error=status` переключает поведение на обычные коды ответа и JSON.

## Тарификация

Заказчик платит за счёт, а не за обращение к API.

Счёт определяется уникальным набором параметров: организация, сумма,
назначение платежа, данные плательщика. Первое обращение с новым набором
создаёт счёт и списывает стоимость генерации. Повторные обращения с теми же
параметрами в течение `INVOICE_FREE_PERIOD_DAYS` не тарифицируются.

Такая модель выбрана потому, что изображение в письме запрашивается
многократно: получатель открывает письмо несколько раз, пересылает его,
почтовый клиент выполняет предзагрузку, антивирус проверяет ссылку.
На 60 отправленных счетов приходится 200–400 обращений.

Готовые изображения не хранятся: повторный запрос содержит те же
параметры, поэтому код формируется заново.

**Овердрафт.** При исчерпании баланса сервис продолжает работу в долг
в течение `OVERDRAFT_DAYS` без ограничения по сумме, чтобы кассовый
разрыв у заказчика не останавливал приём платежей. Овердрафт продлевается
не более `OVERDRAFT_MAX_EXTENSIONS` раз.

**Безлимит.** Заказчику можно отключить тарификацию целиком: счета
создаются и учитываются в статистике, но не списывают средства. Для таких
аккаунтов выписка пуста, а учёт ведётся по числу счетов, обращений к API
и рассчитанной экономии — эти показатели видны в карточке заказчика
вместе с историей счетов.

Все параметры тарификации задаются переменными окружения; полный список
с описаниями — в [`.env.example`](.env.example) и в
[`src/nosbp/core/config.py`](src/nosbp/core/config.py).

## Персональные данные

ФИО и телефон плательщика попадают в QR-код, но **в базе не сохраняются**.
Они участвуют только в вычислении ключа идемпотентности, то есть
преобразуются в необратимый хэш SHA-256.

При этом параметры передаются в строке запроса. Конфигурация nginx из
[`deploy/nginx/nosbp.conf`](deploy/nginx/nosbp.conf) исключает строку
запроса из журнала доступа — без этого данные плательщиков и ключи
заказчиков попадали бы в текстовые логи.

Хранение персональных данных плательщиков остаётся обязанностью заказчика.

## Развёртывание

Инструкция рассчитана на чистый сервер с Ubuntu 24.04 и доменом,
A-запись которого уже указывает на его адрес.

### 1. Подготовка сервера

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y ca-certificates curl git gnupg
```

Конфигурация использует директиву `http2 on`, доступную с nginx 1.25.1.
В репозитории Ubuntu 24.04 находится версия 1.24, поэтому nginx ставится
из официального репозитория разработчиков:

```bash
curl -fsSL https://nginx.org/keys/nginx_signing.key \
  | sudo gpg --dearmor -o /usr/share/keyrings/nginx-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/nginx-archive-keyring.gpg] \
http://nginx.org/packages/mainline/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) nginx" \
  | sudo tee /etc/apt/sources.list.d/nginx.list > /dev/null

# Пакет из этого репозитория имеет приоритет над версией из Ubuntu
printf 'Package: *\nPin: origin nginx.org\nPin-Priority: 900\n' \
  | sudo tee /etc/apt/preferences.d/99nginx > /dev/null

sudo apt update
sudo apt install -y nginx
nginx -v
```

Если nginx уже установлен из репозитория Ubuntu, те же команды обновят
его на свежую версию. Собственные конфигурации в `/etc/nginx/sites-*`
при обновлении сохраняются, но пакет от nginx.org не подключает эти
каталоги автоматически — в конце раздела про nginx это учтено.

Обновление в дальнейшем:

```bash
sudo apt update && sudo apt install --only-upgrade nginx
sudo nginx -t && sudo systemctl reload nginx
```

Установка Docker:

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
```

Отдельный пользователь для сервиса:

```bash
sudo adduser --disabled-password --gecos "" nosbp
sudo usermod -aG docker nosbp
```

Межсетевой экран: наружу открыты только SSH и HTTP(S). Порт приложения
доступен исключительно с самого сервера.

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
```

### 2. Исходный код

```bash
sudo mkdir -p /opt/nosbp && sudo chown nosbp:nosbp /opt/nosbp
sudo -u nosbp git clone https://github.com/ВАШ_АККАУНТ/no-sbp.git /opt/nosbp
cd /opt/nosbp
```

### 3. Объектное хранилище

В хранилище лежат логотипы организаций. Подойдёт любой S3-совместимый
сервис: Yandex Object Storage, Selectel, VK Cloud. Потребуется создать
бакет и сервисный аккаунт с правами на чтение и запись, затем сохранить
идентификатор и секретный ключ.

Бакет должен быть **приватным**: сервис читает логотипы сам и наружу их
не отдаёт.

### 4. Конфигурация

```bash
sudo -u nosbp cp deploy/env.production.example .env
sudo -u nosbp chmod 600 .env
sudo -u nosbp nano .env
```

Обязательно заполняются:

| Переменная | Значение |
|---|---|
| `PUBLIC_BASE_URL` | `https://example.com` — внешний адрес сервиса |
| `POSTGRES_PASSWORD` | Случайный пароль. `openssl rand -base64 24` |
| `DATABASE_URL` | Тот же пароль внутри строки подключения |
| `S3_*` | Адрес, бакет и ключи хранилища |
| `ADMIN_PATH_PREFIX` | Собственный непубличный путь панели, например `/k7f2m9` |
| `ADMIN_ALLOWED_NETWORKS` | Адрес, с которого вы входите в панель |

Путь панели и список сетей должны совпадать с конфигурацией nginx.

### 5. Запуск

```bash
sudo -u nosbp docker compose -f docker-compose.prod.yml build
sudo -u nosbp docker compose -f docker-compose.prod.yml up -d
```

Миграции применяются автоматически при старте контейнера. Проверка:

```bash
curl -s http://127.0.0.1:8000/health
# {"status":"ok"}
```

### 6. nginx

```bash
sudo cp deploy/nginx/proxy_params_nosbp /etc/nginx/proxy_params_nosbp
sudo cp deploy/nginx/nosbp.conf /etc/nginx/sites-available/nosbp.conf
sudo nano /etc/nginx/sites-available/nosbp.conf
```

В файле заменяются три значения:

- `example.com` — домен;
- `/console/` — путь панели, тот же, что в `ADMIN_PATH_PREFIX`;
- `203.0.113.0/24` — сети, которым разрешён доступ к панели. Свой текущий
  адрес можно узнать командой `curl -s ifconfig.me`. Для одного адреса
  указывается он сам: `allow 203.0.113.7;`.

Если адрес у вас динамический, есть два рабочих варианта: разрешить сеть
провайдера целиком или входить в панель через SSH-туннель, оставив
`allow 127.0.0.1`:

```bash
ssh -L 8443:127.0.0.1:443 nosbp@example.com
```

Включение конфигурации:

```bash
sudo mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled
sudo ln -s /etc/nginx/sites-available/nosbp.conf /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default /etc/nginx/conf.d/default.conf
```

Пакет с nginx.org не подключает каталог `sites-enabled`. Если строки
`include /etc/nginx/sites-enabled/*;` в `/etc/nginx/nginx.conf` нет, она
добавляется в блок `http`:

```bash
grep -q "sites-enabled" /etc/nginx/nginx.conf || sudo sed -i \
  "s|include /etc/nginx/conf.d/\*.conf;|include /etc/nginx/conf.d/*.conf;\n    include /etc/nginx/sites-enabled/*;|" \
  /etc/nginx/nginx.conf
sudo nginx -t && sudo systemctl reload nginx
```

### 7. HTTPS

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo mkdir -p /var/www/certbot
sudo certbot --nginx -d example.com
```

Certbot подставит пути к сертификатам и настроит автоматическое продление.
Проверка продления:

```bash
sudo certbot renew --dry-run
```

### 8. Администратор панели

```bash
cd /opt/nosbp
sudo -u nosbp docker compose -f docker-compose.prod.yml exec api \
  nosbp admin create --email you@example.com
```

Команда запросит пароль (дважды, не короче 12 символов) и выведет в
терминал QR-код для приложения-аутентификатора: Google Authenticator,
Aegis, 1Password — любого. Секрет показывается один раз, его следует
сохранить.

После этого панель доступна по адресу `https://example.com/ВАШ_ПУТЬ/login`.

### 9. Оповещения о входе в панель

Сервис отправляет сообщение при каждом входе в панель и при неудачных
попытках. Это позволяет заметить вход, которого вы не совершали.
Поддерживаются Telegram и MAX; можно включить оба сразу.

**Telegram.** Токен выдаёт [@BotFather](https://t.me/BotFather) командой
`/newbot`. Идентификатор чата: напишите созданному боту любое сообщение
и запросите его у [@userinfobot](https://t.me/userinfobot).

**MAX.** Токен выдаёт `@MasterBot`. Идентификатор чата приходит
в обновлениях после первого сообщения боту.

Значения вносятся в `.env`:

```
TELEGRAM_BOT_TOKEN=123456:AA...
TELEGRAM_CHAT_ID=123456789
MAX_BOT_TOKEN=
MAX_CHAT_ID=
```

Канал включается, только если заданы и токен, и идентификатор чата.
Оповещения о неудачных попытках отключаются переменной
`NOTIFY_ADMIN_LOGIN_FAILED=false` — при активном сканировании их бывает
много.

После правки `.env` контейнер перезапускается:

```bash
sudo -u nosbp docker compose -f docker-compose.prod.yml up -d
```

Отправка выполняется после того, как ответ отдан браузеру, и не задерживает
вход. Недоступность мессенджера в журнал попадает, но на работу панели
не влияет.

### 10. Резервное копирование

```bash
sudo -u nosbp crontab -e
```

```
0 4 * * * cd /opt/nosbp && ./docker/backup.sh >> /var/log/nosbp-backup.log 2>&1
```

Копия на том же сервере резервной копией не является. Её следует
отправлять во внешнее хранилище, например через `rclone`:

```
30 4 * * * rclone copy /opt/nosbp/backups remote:nosbp-backups/ --max-age 25h
```

### 11. Первый заказчик

Всё перечисленное доступно в панели; консольные команды удобны для
скриптов и первичной настройки.

```bash
docker compose -f docker-compose.prod.yml exec api nosbp \
  account create --email shop@example.com --name "ООО Ромашка"

docker compose -f docker-compose.prod.yml exec api nosbp org add \
  --email shop@example.com \
  --alias main --name "ООО Ромашка" \
  --account 40702810000000000007 \
  --bank "ПАО Сбербанк" --bic 044525225 \
  --corr 30101810000000000007 \
  --inn 7707083893 --kpp 773601001 \
  --fee 0,7 --color "#0e6b62" --default

docker compose -f docker-compose.prod.yml exec api nosbp \
  token issue --email shop@example.com --label RetailCRM

docker compose -f docker-compose.prod.yml exec api nosbp \
  topup --email shop@example.com --rubles 1000
```

Реквизиты проверяются по контрольному разряду: опечатка в номере счёта
не пройдёт.

## Эксплуатация

### Обновление

```bash
cd /opt/nosbp
sudo -u nosbp ./docker/backup.sh
sudo -u nosbp git pull
sudo -u nosbp docker compose -f docker-compose.prod.yml build
sudo -u nosbp docker compose -f docker-compose.prod.yml up -d
```

Миграции применяются при старте контейнера. Текущая версия схемы:

```bash
docker compose -f docker-compose.prod.yml exec api alembic current
```

### Перенос на другой сервер

База переносится дампом. Копировать том PostgreSQL не следует: формат
данных на диске несовместим между версиями сервера. Логотипы находятся
во внешнем объектном хранилище и переноса не требуют.

```bash
# На прежнем сервере
./docker/backup.sh
scp backups/nosbp-20260902-040000.dump new-server:/opt/nosbp/backups/

# На новом сервере: пройти шаги 1–7, затем
./docker/restore.sh backups/nosbp-20260902-040000.dump
docker compose -f docker-compose.prod.yml exec api alembic current
```

Восстановление хотя бы раз следует проверить заранее: непроверенная
резервная копия резервной копией не является.

### Управление администраторами

```bash
nosbp admin list                      # список
nosbp admin password --email you@…    # смена пароля, закрывает все сессии
nosbp admin reset-totp --email you@…  # перевыпуск второго фактора
nosbp admin unlock --email you@…      # снятие блокировки после подбора
nosbp admin disable --email you@…     # отключение
```

### Журналы

```bash
docker compose -f docker-compose.prod.yml logs -f api
sudo tail -f /var/log/nginx/nosbp-access.log
```

Логи контейнеров ротируются автоматически: пять файлов по 10 МБ.

## Модель доступа

Ключ API виден в адресе изображения, а значит, доступен получателю письма.
Риск ограничен: реквизиты получателя привязаны к аккаунту и в запросе не
передаются, поэтому подменить их нельзя. Компрометация ключа позволяет
только расходовать баланс заказчика генерацией счетов с произвольными
параметрами.

Ограничивают ущерб:

- суточный потолок списаний — по умолчанию 500 ₽ для новых аккаунтов,
  снимается флагом `--daily-limit 0`;
- немедленный отзыв ключа: `nosbp token revoke --prefix XXXXXXXX`;
- несколько ключей на аккаунт — по одному на интеграцию, чтобы отзывать
  их по отдельности;
- хранение только хэша ключа в базе.

### Защита панели управления

| Уровень | Механизм |
|---|---|
| Сеть | Список разрешённых адресов в nginx и в `ADMIN_ALLOWED_NETWORKS` |
| Адрес | Непубличный путь из `ADMIN_PATH_PREFIX` |
| Пароль | argon2id, минимум 12 символов |
| Второй фактор | Одноразовый код TOTP, обязателен по умолчанию |
| Подбор | 5 неудач подряд — блокировка на 15 минут, плюс лимит в nginx |
| Сессия | Хранится в базе, отзывается немедленно; 12 часов, 60 минут без действий |
| Кука | HttpOnly, Secure, SameSite=Strict |
| Формы | Токен CSRF, сравнение за постоянное время |
| Страницы | CSP без встроенных скриптов и стилей, `X-Frame-Options: DENY` |
| Оповещения | Сообщение в Telegram или MAX при каждом входе и неудачной попытке |

Учётные записи администраторов создаются только из консоли. Смена пароля
и перевыпуск второго фактора закрывают все открытые сессии.

В рабочем окружении интерактивная документация API (`/docs`, `/redoc`,
`/openapi.json`) не отдаётся.

## Разработка

Требуются Docker, Poetry и Python 3.13 или новее.

```bash
git clone https://github.com/ВАШ_АККАУНТ/no-sbp.git
cd no-sbp
cp .env.example .env

docker compose up -d postgres minio
poetry install --with dev
poetry run alembic upgrade head
poetry run uvicorn nosbp.main:app --reload
```

Документация API — http://127.0.0.1:8000/docs

### Тесты

Тесты выполняются на PostgreSQL: корректность списаний обеспечивается
блокировкой строки `SELECT ... FOR UPDATE`, которая в SQLite не работает.

```bash
docker compose up -d postgres
docker exec nosbp-postgres psql -U nosbp -d postgres \
  -c "CREATE DATABASE nosbp_test OWNER nosbp"
poetry run pytest
```

Отдельная группа тестов читает сформированный QR-код программным сканером
(с логотипом, цветом и кириллической строкой), другая проверяет, что
параметры тарификации берутся из настроек, а не заданы в коде.

### Проверки перед коммитом

```bash
poetry run ruff check . && poetry run ruff format --check . && poetry run mypy && poetry run pytest
```

Те же проверки выполняются в GitHub Actions на каждый push и pull request.

### Миграции

```bash
poetry run alembic revision --autogenerate -m "описание изменения"
poetry run alembic upgrade head
```

Колонки `NOT NULL`, добавляемые в непустые таблицы, требуют
`server_default` в миграции; после заполнения умолчание снимается.
Пример — в [`alembic/versions`](alembic/versions).

## Устройство проекта

```
src/nosbp/
  admin/       панель управления: вход, CRUD, статистика, шаблоны, статика
  billing/     баланс, списания, овердрафт, журнал операций
  core/        настройки, константы, деньги, логирование, ошибки, middleware
  db/          модели и подключение к базе
  invoices/    поиск или создание счёта
  notifications/ оповещения в Telegram и MAX
  payments/    строка ГОСТ, отрисовка QR, проверка реквизитов, эндпоинты
  scripts/     консольная утилита администрирования
  storage/     объектное хранилище логотипов
alembic/       миграции схемы
deploy/        конфигурация nginx и шаблон окружения
docker/        точка входа контейнера, резервное копирование
tests/         тесты, выполняются на PostgreSQL
```

### Технологии

Python 3.13, FastAPI, SQLAlchemy 2 (async), PostgreSQL 17, Alembic,
segno, Pillow, boto3, httpx, structlog, Jinja2, argon2-cffi, pyotp.
Проверки: ruff, mypy в строгом режиме, pytest.

## Лицензия

[MIT](LICENSE)
