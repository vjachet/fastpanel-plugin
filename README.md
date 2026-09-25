# fastpanel — плагин Claude Code

Набор инструментов для работы с сервером FastPanel/FluxPanel из агента. Всё делается от
имени обычного пользователя панели — тем же доступом, каким ты заходишь в её интерфейс.
Root и админ панели не нужны.

## Что внутри

| Скилл | Что умеет |
|---|---|
| `fastpanel-accounts` | какие аккаунты панели настроены; кто мы на панели (user id, home, владелец, квота) |
| `fastpanel-sites` | список сайтов с их id; подробности одного сайта; создание сайта (PHP модулем Apache или FastCGI с выбором версии, статика или обратный прокси для Node.js) с выбором IP, сжатия, кеша статики и логов, с сертификатом Let's Encrypt; довыпуск сертификата, когда DNS готов |
| `fastpanel-db` | список баз; серверы БД и кодировки; идемпотентное создание базы с пользователем и привязкой к сайту |

Общее ядро — `plugins/fastpanel/lib/fastpanel_api.py`: конфиг, логин, кеш токена, разбор
ошибок панели. Новый инструмент добавляется маленьким скриптом поверх него.

## Пароль панели

Пароль лежит **только** в локальном файле с правами `600` и уходит **только** на `/login`
твоей панели. Ни в аргументах команд, ни в выводе, ни в переписке с Claude он не появляется —
скрипты читают его сами, и все сообщения об ошибках проходят через скрабер. Файл вне
репозиториев, так что случайно не уедет в git или на сервер.

## Установка

```
/plugin marketplace add https://github.com/vjachet/fastpanel-plugin
/plugin install fastpanel@fastpanel
```

Нужен `python3` (3.8+). Внешних зависимостей нет. Подробный порядок установки, настройки
и проверки — в [INSTALL.md](INSTALL.md). Сборка архива для передачи:

```bash
git archive --format=tar.gz --prefix=fastpanel-plugin/ -o dist/fastpanel-plugin-2.1.0.tar.gz HEAD
```

## Настройка доступов

Один файл `~/.config/fastpanel/config.json` на все панели и всех пользователей. Делаешь
руками, в своём терминале — не через Claude:

```bash
install -d -m 700 ~/.config/fastpanel
cat > ~/.config/fastpanel/config.json <<'JSON'
{
  "url": "panel.example.com",
  "accounts": [
    {"login": "user1", "password": "secret1", "label": "рабочий"},
    {"login": "user2", "password": "secret2"}
  ]
}
JSON
chmod 600 ~/.config/fastpanel/config.json
```

`url` — хост панели, как набираешь его в браузере. `https://` подставляется само, `/api`
скрипты дописывают сами, одинаково для всех панелей; нестандартный порт — прямо в хосте
(`panel.example.com:8888`). Аккаунт называется своим логином; хочешь короче — добавь
`"name": "work"`. У панели самоподписанный сертификат — `"insecure": true`. Вторая панель —
задай аккаунту свой `"url"`. Другой путь к файлу — переменная `FASTPANEL_CONFIG`.

Файла нет — скрипт при запуске сам печатает путь, команды создания и пример содержимого.

Проверка:

```bash
find ~/.claude/plugins -name fastpanel_accounts.py -exec python3 {} list \;
```

Пароли эта команда не печатает — только имя аккаунта, URL панели, логин и метку.

## Использование

Обычно просто говоришь Claude, что нужно. Напрямую:

```bash
P=~/.claude/plugins/.../plugins/fastpanel/skills

python3 $P/fastpanel-accounts/scripts/fastpanel_accounts.py list
python3 $P/fastpanel-accounts/scripts/fastpanel_accounts.py whoami -A work
python3 $P/fastpanel-sites/scripts/fastpanel_sites.py list -A work
python3 $P/fastpanel-sites/scripts/fastpanel_sites.py options -A work
python3 $P/fastpanel-sites/scripts/fastpanel_sites.py add shop.example.com -A work --handler fcgi --php 74
python3 $P/fastpanel-sites/scripts/fastpanel_sites.py ssl shop.example.com -A work
python3 $P/fastpanel-db/scripts/fastpanel_db.py servers -A work
python3 $P/fastpanel-db/scripts/fastpanel_db.py add shop_main -A work --site shop.example.com
python3 $P/fastpanel-db/scripts/fastpanel_db.py add analytics -A work --server pg15 --charset cp1251
```

Если аккаунт один — `-A` можно не писать. Если несколько и ни у кого нет
`"name": "default"`, скрипт не угадывает: показывает список и просит выбрать.

## Если имя базы занято

База с таким именем уже есть у тебя — `add` молча выходит с кодом 0, ничего не трогая.
Имя занято другим пользователем панели (панель отвечает `errors.name: «База данных ... уже
существует»`) — скрипт в чужую базу не лезет: предлагает несколько свободных имён и выходит
с кодом 3. В терминале он покажет меню с пунктом «ввести своё название», а Claude в этом
случае спросит тебя, какое имя брать.

## Где оказываются доступы к созданной БД

`~/.config/fastpanel/databases/<аккаунт>/<имя_базы>.env`, права `600`:

```
DB_ENGINE=mysql
DB_HOST=127.0.0.1
DB_PORT=3306
DB_NAME=shop_main
DB_USER=shop_main
DB_PASSWORD=...
```

Пароль БД в вывод не печатается намеренно — иначе он попал бы в переписку с моделью.
Смотри файл сам.

## Коды возврата

| Код | Значение |
|---|---|
| 0 | сделано (или уже было сделано раньше) |
| 1 | ошибка: нет доступов, кривые права на файл, не выбран аккаунт, не вышел логин, панель отказала |
| 3 | имя базы занято другим пользователем панели — нужен выбор пользователя |

## Структура

```
.claude-plugin/marketplace.json
plugins/fastpanel/
  .claude-plugin/plugin.json
  SETUP.md                     общее описание конфига и правил с паролем
  lib/fastpanel_api.py         конфиг, логин, http, разбор ошибок
  skills/fastpanel-accounts/   SKILL.md + scripts/fastpanel_accounts.py
  skills/fastpanel-sites/      SKILL.md + scripts/fastpanel_sites.py
  skills/fastpanel-db/         SKILL.md + scripts/fastpanel_db.py
```

## Известные эндпоинты панели

Проверено на живой панели: `POST /login`, `GET /api/users`, `GET /api/charsets`,
`GET /api/databases?offset=&limit=`, `POST /api/databases`, `GET /api/databases/servers`,
`GET /api/databases/servers/<id>/users`, `GET /api/sites/list?filter[...]`,
`GET /api/sites/simple`, `GET /api/sites/<id>`, `GET /api/me`, `GET /api/settings`
(версии PHP и IP), `POST /api/master/domain`, `PUT /api/master` (создание сайта), `PUT /api/sites/backend/<id>` (бэкенд),
`PUT /api/sites/<id>` (сжатие, кеш статики, HTTPS), `GET|PUT /api/sites/<id>/log_rotate`,
`POST /api/certificates`, `GET /api/certificates/<id>`.
Эндпоинтов `/api/users/me`, `/api/current_user` и `/api/sites` нет.

У нового сайта по умолчанию: логирование посещений выключено, ротация 0 копий,
gzip 5, кеш статики 14 дней, сертификат Let's Encrypt на `admin@<домен>` с редиректом на
HTTPS, HSTS, HTTP/2 и HTTP/3. Всё меняется флагами, список — `fastpanel_sites.py options`.
