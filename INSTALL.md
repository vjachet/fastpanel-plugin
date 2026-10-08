# Установка плагина fastpanel

Плагин для Claude Code: работа с сервером FastPanel/FluxPanel от имени обычного
пользователя панели. Root не нужен; создание пользователей требует права на эту операцию.

Нужен `python3` версии 3.8 или новее. Внешних библиотек нет.

## 1. Получить файлы

**Из архива:**

```bash
sha256sum -c fastpanel-plugin-3.0.0.tar.gz.sha256   # если прислали файл с суммой
mkdir -p ~/plugins && tar xzf fastpanel-plugin-3.0.0.tar.gz -C ~/plugins
# получится ~/plugins/fastpanel-plugin
```

**Из git:**

```bash
git clone https://github.com/vjachet/fastpanel-plugin.git ~/plugins/fastpanel-plugin
```

## 2. Подключить в Claude Code

В любой сессии Claude Code:

```
/plugin marketplace add ~/plugins/fastpanel-plugin
/plugin install fastpanel@fastpanel
```

Шаг 1 можно пропустить — вместо локального пути укажи адрес репозитория:

```
/plugin marketplace add https://github.com/vjachet/fastpanel-plugin
/plugin install fastpanel@fastpanel
```

Проверить, что скиллы видны: `/plugin` → в списке должны быть `fastpanel-accounts`,
`fastpanel-sites`, `fastpanel-db`, `fastpanel-dns`.

## 3. Прописать доступы к панели

Один файл на все панели и всех пользователей: `~/.config/fastpanel/config.json`.
Создай его **сам, в своём терминале** — не через Claude, чтобы логин и пароль не попали в переписку.

```bash
install -d -m 700 ~/.config/fastpanel
cat > ~/.config/fastpanel/config.json <<'JSON'
{
  "url": "panel.example.com",
  "accounts": [
    {"login": "user1", "password": "secret1", "label": "рабочий"}
  ]
}
JSON
chmod 600 ~/.config/fastpanel/config.json
```

- `url` — хост панели, как набираешь его в браузере. `https://` подставляется само,
  `/api` скрипты дописывают сами. Нестандартный порт пишется в хосте:
  `panel.example.com:8888`.
- Логин нигде не печатается. Аккаунт получает имя-хеш вида `acc-1a2b3c4d` — по нему он
  выбирается (`-A acc-1a2b3c4d`) и показывается. `name` и `label` остаются локально;
  человек может получить id командой `account-id -A ПСЕВДОНИМ` и передать агенту только id.
- `login` и `password` — те же, с которыми заходишь в интерфейс панели.
- Несколько пользователей или несколько панелей — добавь ещё объектов в `accounts`;
  у аккаунта может быть свой `"url"`.
- Самоподписанный сертификат у панели — добавь в файл `"insecure": true`.
- Права `600` обязательны: скрипт откажется читать файл, доступный группе или другим.

Файл лежит вне репозиториев и проектов, так что не уедет в git или на сервер.

## 4. Проверить

```bash
find ~/.claude/plugins -name fastpanel_accounts.py -exec python3 {} list \;
```

Должен напечататься JSON с `account_ids`, без URL, псевдонимов и доступов.
Затем — проверка авторизации в панели:

```bash
find ~/.claude/plugins -name fastpanel_accounts.py -exec python3 {} whoami \;
```

Если доступы верные, увидишь только JSON с `user_id`.

## 5. Пользоваться

Обычно просто говоришь Claude, что нужно: «покажи сайты на панели», «список баз»,
«заведи базу shop_main для сайта shop.example.com». Он сам выберет нужный скилл.

Напрямую, если надо:

```bash
P=$(find ~/.claude/plugins -type d -name fastpanel -path '*plugins/fastpanel')/skills

python3 $P/fastpanel-accounts/scripts/fastpanel_accounts.py list
python3 $P/fastpanel-accounts/scripts/fastpanel_accounts.py add --request ID_ЗАЯВКИ -A acc-1a2b3c4d --quota 0
python3 $P/fastpanel-accounts/scripts/fastpanel_accounts.py ssh-add 93 -A acc-1a2b3c4d --key-file ~/.ssh/id_ed25519.pub
python3 $P/fastpanel-sites/scripts/fastpanel_sites.py list
python3 $P/fastpanel-db/scripts/fastpanel_db.py servers
python3 $P/fastpanel-db/scripts/fastpanel_db.py add shop_main --site shop.example.com
```

Доступы к созданной базе кладутся в `~/.config/fastpanel/databases/<аккаунт>/<имя>.env`
(права `600`). Пароль базы в вывод не печатается намеренно — смотри файл сам.

Имя нового пользователя не передаётся агенту. В своём терминале запусти
`python3 fastpanel_accounts.py prepare-user`: ввод скрыт, файл заявки имеет права `600`.
Агенту передай только выданный id заявки; он вызовет `add --request ID_ЗАЯВКИ`.
Для существующих пользователей все команды принимают только числовые id.

`fastpanel-accounts add` создаёт обычного пользователя (`ROLE_USER`) через аккаунт
с правом создания пользователей. Повторный вызов для существующего логина ничего
не меняет. Новые доступы — в `~/.config/fastpanel/users/<аккаунт>/user-<суффикс>.json`
(`600`), агент их не читает; в `config.json` они автоматически не добавляются.

## Обновление

Если плагин ставился из локальной копии — сначала обнови её:

```bash
cd ~/plugins/fastpanel-plugin && git pull      # или распакуй новый архив поверх
```

Затем, в терминале:

```bash
claude plugin marketplace update fastpanel
claude plugin update fastpanel@fastpanel
```

Новая версия подхватится при следующем запуске Claude Code или сразу после `/reload-plugins`.

Сам плагин не обновляется: у сторонних маркетплейсов автообновление по умолчанию
выключено. Включить: `/plugin` → вкладка **Marketplaces** → `fastpanel` →
**Enable auto-update**. Тогда проверка идёт в фоне после старта сессии, а новая
версия загружается при следующем запуске.

## Удаление

```
/plugin uninstall fastpanel@fastpanel
/plugin marketplace remove fastpanel
```

Файл с доступами и выгруженные пароли баз остаются на месте — удали руками, если не нужны:
`rm -rf ~/.config/fastpanel ~/.cache/fastpanel`.

## Если что-то не работает

Агент получает только `{"status":"error","code":"..."}`. При `account_required`
нужно выбрать id из `list`. При `owner_required` — числовой id из `owner_ids`.
`operation_unconfirmed` означает, что результат не подтверждён: сначала проверь состояние,
не повторяй изменение вслепую.

Подробности остальных ошибок человек смотрит **локально, вне агента** в
`~/.config/fastpanel/diagnostics/last-error.json` (каталог `700`, файл `600`).
Путь не передаётся в ответе; при FASTPANEL_CONFIG_DIR используется этот каталог.
Не отправляй файл агенту. Проверь настройку из шага 3, права `600`, доступность панели
и вход в браузере. Диагностика может содержать адрес панели и внутренние пути.
