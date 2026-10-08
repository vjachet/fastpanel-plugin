# fastpanel — плагин Claude Code

Набор инструментов для работы с сервером FastPanel/FluxPanel из агента. Всё делается от
имени обычного пользователя панели — тем же доступом, каким ты заходишь в её интерфейс.
Root не нужен. Для сайтов и баз достаточно обычного пользователя панели;
для создания пользователей нужен аккаунт с соответствующим правом.

## Что внутри

| Скилл | Что умеет |
|---|---|
| `fastpanel-accounts` | какие аккаунты панели настроены; наш user id; список id пользователей; создание обычного пользователя с генерируемым паролем и квотой; список и добавление публичных SSH-ключей пользователю |
| `fastpanel-sites` | список сайтов с их id; подробности одного сайта; создание сайта (PHP модулем Apache или FastCGI с выбором версии, статика или обратный прокси для Node.js) с выбором IP, сжатия, кеша статики и логов, с сертификатом Let's Encrypt; смена обработчика и версии PHP; довыпуск сертификата, когда DNS готов; что привязано к сайту; удаление сайта (по выбору — с базами, DNS-доменами и их зонами у провайдера, почтой, поддоменами) |
| `fastpanel-db` | список баз; серверы БД и кодировки; идемпотентное создание базы с пользователем и привязкой к сайту |
| `fastpanel-dns` | DNS-аккаунты панели (внешние провайдеры: FASTDNS, Cloudflare); список DNS-доменов и на каком аккаунте каждый; записи домена; идемпотентное добавление DNS-домена на выбранный аккаунт, с сайтом или без; синхронизация записей домена с аккаунтом; добавление записи в домен, изменение её значения и удаление; привязка почты домена к сервису (Google); удаление DNS-домена из панели, по выбору — вместе с зоной у провайдера |

Общее ядро — `plugins/fastpanel/lib/fastpanel_api.py`: конфиг, логин, кеш токена, разбор
ошибок панели. Новый инструмент добавляется маленьким скриптом поверх него.

## Логин, пароль и токен панели

Логин и пароль лежат **только** в локальном файле с правами `600` и уходят **только** на
`/login` твоей панели; полученный токен — только в кеше `~/.cache/fastpanel/` (`600`). Ни в
аргументах команд, ни в выводе, ни в переписке с Claude они не появляются: скрипты читают их
сами, а свободный вывод (stdout и stderr) проходит через скрабер, который заменяет логин,
пароль и токен на `***`. Файл вне репозиториев, так что случайно не уедет в git или на сервер.

Имена всех видимых пользователей скрываются, в том числе администратора.
Список пользователей содержит только id, список SSH-ключей — только id пользователя и ключей.
Команды пользователей и SSH выдают компактный JSON: id, результат операции и необходимое
состояние доступа. URL панели, локальные псевдонимы, пути, роли, квоты, отпечатки и комментарии
остаются внутри плагина. Ошибки — фиксированный code; диагностика сохраняется локально в
`~/.config/fastpanel/diagnostics/last-error.json` (`600`), агент её не читает.
Скрабер режет логин везде, в том числе в данных самой панели: корень сайта выглядит как
`/var/www/***/data/www/...`, база `<логин>_main` — как `***_main`. Короткий логин может
замазать совпадающий кусок домена.

Агенту закрыт и прямой доступ: хук плагина (`plugins/fastpanel/hooks/guard.py`) отклоняет
Read/Edit/Write/Grep/Glob и shell-команды, которые лезут в `~/.config/fastpanel/` и
`~/.cache/fastpanel/` или подключают ядро в обход скриптов. Панель принимается только по
`https`, редиректы не выполняются. Выключателя у скрабера и хука нет.

Чего хук не гарантирует: shell-команду он видит как текст и ловит по шаблонам, а агент
работает под тем же пользователем ОС, которому принадлежит файл. Прямые обращения закрыты,
изощрённый обход — нет. Жёсткая граница — только на уровне ОС.

## Владелец сайта и базы

Аккаунт, который видит только себя, создаёт под собой. Аккаунт, который видит нескольких
пользователей панели (администратор `fastpanel`), обязан назвать владельца: `--owner` (только числовой id пользователя), иначе код 4 и список пользователей. От имени самого `fastpanel` ничего
не создаётся. База создаётся только с `--site`, и сайт должен принадлежать тому же
пользователю, что и база.

## Установка

```
/plugin marketplace add https://github.com/vjachet/fastpanel-plugin
/plugin install fastpanel@fastpanel
```

Нужен `python3` (3.8+). Внешних зависимостей нет. Подробный порядок установки, настройки
и проверки — в [INSTALL.md](INSTALL.md). Сборка архива для передачи:

```bash
git archive --format=tar.gz --prefix=fastpanel-plugin/ -o dist/fastpanel-plugin-3.0.0.tar.gz HEAD
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
    {"login": "user2", "password": "secret2", "name": "second"}
  ]
}
JSON
chmod 600 ~/.config/fastpanel/config.json
```

`url` — хост панели, как набираешь его в браузере. `https://` подставляется само, `/api`
скрипты дописывают сами, одинаково для всех панелей; нестандартный порт — прямо в хосте
(`panel.example.com:8888`). Логином аккаунт не называется: скрипт сам даёт ему имя-хеш вида
`acc-1a2b3c4d` (видно в `fastpanel_accounts.py list`), оно идёт в `-A` и в имена файлов.
Локальный псевдоним — полем `"name"`, только не логин и без логина внутри.
Агенту передаётся только id: человек получает его через `account-id -A ПСЕВДОНИМ`. У панели самоподписанный сертификат — `"insecure": true`. Вторая панель —
задай аккаунту свой `"url"`. Другой путь к файлу — переменная `FASTPANEL_CONFIG`.

Файла нет — скрипт выдаёт код ошибки; человек настраивает доступы по INSTALL.md.
Подробности ошибки доступны человеку только в локальной диагностике.

Проверка:

```bash
find ~/.claude/plugins -name fastpanel_accounts.py -exec python3 {} list \;
```

Команда печатает только JSON `account_ids`, без адресов панелей и локальных псевдонимов.

## Использование

Обычно просто говоришь Claude, что нужно. Напрямую:

```bash
P=~/.claude/plugins/.../plugins/fastpanel/skills

python3 $P/fastpanel-accounts/scripts/fastpanel_accounts.py list
python3 $P/fastpanel-accounts/scripts/fastpanel_accounts.py whoami -A acc-1a2b3c4d
python3 $P/fastpanel-accounts/scripts/fastpanel_accounts.py add --request ID_ЗАЯВКИ -A acc-1a2b3c4d --quota 0
python3 $P/fastpanel-accounts/scripts/fastpanel_accounts.py ssh-keys 93 -A acc-1a2b3c4d
python3 $P/fastpanel-accounts/scripts/fastpanel_accounts.py ssh-add 93 -A acc-1a2b3c4d --key-file ~/.ssh/id_ed25519.pub --desc 'work laptop'
python3 $P/fastpanel-sites/scripts/fastpanel_sites.py list -A acc-1a2b3c4d
python3 $P/fastpanel-sites/scripts/fastpanel_sites.py options -A acc-1a2b3c4d
python3 $P/fastpanel-sites/scripts/fastpanel_sites.py add shop.example.com -A acc-1a2b3c4d --handler fcgi --php 74
python3 $P/fastpanel-sites/scripts/fastpanel_sites.py backend shop.example.com -A acc-1a2b3c4d --handler fcgi --php 82
python3 $P/fastpanel-sites/scripts/fastpanel_sites.py ssl shop.example.com -A acc-1a2b3c4d
python3 $P/fastpanel-sites/scripts/fastpanel_sites.py resources old.example.com -A acc-1a2b3c4d
python3 $P/fastpanel-sites/scripts/fastpanel_sites.py delete old.example.com -A acc-1a2b3c4d --dns-provider                  # показать, что привязано, и код
python3 $P/fastpanel-sites/scripts/fastpanel_sites.py delete old.example.com -A acc-1a2b3c4d --dns-provider --confirm КОД    # удалить
python3 $P/fastpanel-db/scripts/fastpanel_db.py servers -A acc-1a2b3c4d
python3 $P/fastpanel-db/scripts/fastpanel_db.py add shop_main -A acc-1a2b3c4d --site shop.example.com
python3 $P/fastpanel-db/scripts/fastpanel_db.py add analytics -A acc-1a2b3c4d --site shop.example.com --server pg15 --charset cp1251
python3 $P/fastpanel-dns/scripts/fastpanel_dns.py accounts -A acc-1a2b3c4d
python3 $P/fastpanel-dns/scripts/fastpanel_dns.py list -A acc-1a2b3c4d
python3 $P/fastpanel-dns/scripts/fastpanel_dns.py add shop.example.com -A acc-1a2b3c4d --dns-account fastDNSfree
python3 $P/fastpanel-dns/scripts/fastpanel_dns.py sync shop.example.com -A acc-1a2b3c4d
python3 $P/fastpanel-dns/scripts/fastpanel_dns.py show shop.example.com -A acc-1a2b3c4d
python3 $P/fastpanel-dns/scripts/fastpanel_dns.py mailto shop.example.com google -A acc-1a2b3c4d
python3 $P/fastpanel-dns/scripts/fastpanel_dns.py record-add shop.example.com www A 203.0.113.10 -A acc-1a2b3c4d
python3 $P/fastpanel-dns/scripts/fastpanel_dns.py delete old.example.com -A acc-1a2b3c4d --from-provider                  # показать записи и код
python3 $P/fastpanel-dns/scripts/fastpanel_dns.py delete old.example.com -A acc-1a2b3c4d --from-provider --confirm КОД    # удалить
python3 $P/fastpanel-accounts/scripts/fastpanel_accounts.py users -A acc-1a2b3c4d
python3 $P/fastpanel-sites/scripts/fastpanel_sites.py add new.example.com -A acc-1a2b3c4d --owner 93
```

Если аккаунт один — `-A` можно не писать. Если несколько и ни у кого нет
`"name": "default"`, скрипт не угадывает: возвращает `account_required`; получи список id через `list`.

## Создание пользователя панели

Имя нового пользователя не передаётся агенту. В своём терминале запусти
`python3 fastpanel_accounts.py prepare-user`: ввод скрыт, файл заявки имеет права `600`.
Агенту передай только выданный id заявки; он вызовет `add --request ID_ЗАЯВКИ`.
Для существующих пользователей все команды принимают только числовые id.

`fastpanel-accounts add --request ID_ЗАЯВКИ -A ID_АККАУНТА [--quota N] [--wait N]` создаёт `ROLE_USER`
через `POST /api/users`. Логин уже есть — ничего не меняет, включая пароль и квоту.
Пароль генерируется, а доступы сохраняются до запроса в
`~/.config/fastpanel/users/<аккаунт>/user-<суффикс>.json` (`600`); агент файл не читает.
В выводе — только status и user_id. Путь не печатается; человек находит файл по
сохранённому request_id. В локальный `config.json`
новый аккаунт не добавляется. Квота по умолчанию `0`; значение API передаётся без
пересчёта единиц. Скрипт ждёт появления пользователя до 60 секунд.
Ошибка/тайм-аут — сначала проверить `users`: задача могла завершиться после потери
ответа. Сохранённый файл после отказа содержит лишь доступы из попытки создания.

## SSH-ключи пользователя

`fastpanel-accounts ssh-add ПОЛЬЗОВАТЕЛЬ -A ID_АККАУНТА --key-file ПУТЬ.pub [--desc ОПИСАНИЕ]`
добавляет публичный ключ через API панели. Можно передать `--key 'ssh-ed25519 ...'`
вместо файла. Пользователь выбирается только по числовому id
на выбранной панели. Описание по умолчанию — комментарий ключа.
Приватные ключи не принимаются. Один и тот же ключ не дублируется при смене комментария;
существующие ключи и их настройки не меняются. `ssh-keys ПОЛЬЗОВАТЕЛЬ` выдаёт только key_ids.
Успех: `{"status":"added","user_id":93}`; существующий ключ: status `exists`.
`ssh-status ПОЛЬЗОВАТЕЛЬ` проверяет, включён ли пользователь, разрешён ли ему SSH
(`ssh_access`) и есть ли активный публичный ключ. В ответе только user_id и ssh_ready,
при отказе — фиксированная reason. До и после `ssh-add` проверяются
`enabled` и `ssh_access`; при отключённом доступе команда возвращает ошибку.
После добавления скрипт до 60 секунд ждёт включённый ключ без задачи в очереди.
SSH-доступ включается отдельно в панели; автоматическое включение пока не реализовано.
Реальный вход выполняется с компьютера владельца приватного ключа. Логин и команду
подключения человек узнаёт локально; агенту передаётся только id пользователя.

## Удаление — только после проверки

`delete` сайта и DNS-домена и `record-delete` записи без `--confirm` ничего не удаляют:
показывают, что привязано (базы, DNS-домены, почта, поддомены; для DNS-домена — его записи;
для записи — её саму), и печатают код. Удаление —
та же команда с `--confirm КОД`. Код перестаёт подходить, если привязанное или выбранные
флаги изменились, так что удалить то, что не было показано, нельзя.

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
| 4 | аккаунт видит нескольких пользователей панели, владелец (`--owner`) не назван |
| 5 | DNS-аккаунтов на панели несколько, а `--dns-account` не назван |

## Структура

```
.claude-plugin/marketplace.json
plugins/fastpanel/
  .claude-plugin/plugin.json
  hooks/hooks.json, guard.py   хук: агенту нет хода к доступам и кешу токена
  SETUP.md                     общее описание конфига и правил с логином, паролем и токеном
  lib/fastpanel_api.py         конфиг, логин, http, разбор ошибок
  skills/fastpanel-accounts/   SKILL.md + scripts/fastpanel_accounts.py
  skills/fastpanel-sites/      SKILL.md + scripts/fastpanel_sites.py
  skills/fastpanel-db/         SKILL.md + scripts/fastpanel_db.py
  skills/fastpanel-dns/        SKILL.md + scripts/fastpanel_dns.py
```

## Известные эндпоинты панели

Проверено на живой панели: `POST /login`, `GET /api/users`, `GET /api/charsets`,
`GET /api/databases?offset=&limit=`, `POST /api/databases`, `GET /api/databases/servers`,
`GET /api/databases/servers/<id>/users`, `GET /api/sites/list?filter[...]`,
`GET /api/sites/simple`, `GET /api/sites/<id>`, `GET /api/me`, `GET /api/settings`
(версии PHP и IP), `POST /api/master/domain`, `PUT /api/master` (создание сайта), `GET|PUT /api/sites/backend/<id>` (бэкенд),
`PUT /api/sites/<id>` (сжатие, кеш статики, HTTPS), `GET|PUT /api/sites/<id>/log_rotate`,
`POST /api/certificates`, `GET /api/certificates/<id>`, `GET /api/dns/account`,
`GET|POST /api/dns/domains`, `GET /api/dns/domain/<id>/records`, `PUT /api/dns/domains/<id>/refresh`,
`GET /api/sites/<id>/resources`, `PUT /api/sites/<id>/delete`,
`PUT /api/dns/domains/<id>/mailto` (снято с интерфейса панели, скриптом не запускалось),
`POST /api/dns/domain/<id>/records`, `PUT /api/dns/domain/records/<id записи>`,
`DELETE /api/dns/domain/records/<id записи>` (снято с интерфейса панели, скриптом не запускалось),
`DELETE /api/dns/domains/<id>?removeFromProvider=true` (с `false` снято с интерфейса панели, скриптом
не запускалось).
Эндпоинтов `/api/users/me`, `/api/current_user` и `/api/sites` нет.

У нового сайта по умолчанию: логирование посещений выключено, ротация 0 копий,
gzip 5, кеш статики 14 дней, сертификат Let's Encrypt на `admin@<домен>` с редиректом на
HTTPS, HSTS, HTTP/2 и HTTP/3. Всё меняется флагами, список — `fastpanel_sites.py options`.
