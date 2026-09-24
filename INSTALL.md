# Установка плагина fastpanel

Плагин для Claude Code: работа с сервером FastPanel/FluxPanel от имени обычного
пользователя панели. Root и админ панели не нужны.

Нужен `python3` версии 3.8 или новее. Внешних библиотек нет.

## 1. Получить файлы

**Из архива:**

```bash
sha256sum -c fastpanel-plugin-2.0.0.tar.gz.sha256   # если прислали файл с суммой
mkdir -p ~/plugins && tar xzf fastpanel-plugin-2.0.0.tar.gz -C ~/plugins
# получится ~/plugins/fastpanel-plugin
```

**Из git:**

```bash
git clone <url-репозитория> ~/plugins/fastpanel-plugin
```

## 2. Подключить в Claude Code

В любой сессии Claude Code:

```
/plugin marketplace add ~/plugins/fastpanel-plugin
/plugin install fastpanel@fastpanel
```

Вместо локального пути в первой команде можно указать git-url репозитория — тогда шаг 1
не нужен.

Проверить, что скиллы видны: `/plugin` → в списке должны быть `fastpanel-accounts`,
`fastpanel-sites`, `fastpanel-db`.

## 3. Прописать доступы к панели

Один файл на все панели и всех пользователей: `~/.config/fastpanel/config.json`.
Создай его **сам, в своём терминале** — не через Claude, чтобы пароль не попал в переписку.

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
- `login` и `password` — те же, с которыми заходишь в интерфейс панели.
- Несколько пользователей или несколько панелей — добавь ещё объектов в `accounts`;
  у аккаунта может быть свой `"url"`. Короткое имя аккаунта — `"name": "work"`.
- Самоподписанный сертификат у панели — добавь в файл `"insecure": true`.
- Права `600` обязательны: скрипт откажется читать файл, доступный группе или другим.

Файл лежит вне репозиториев и проектов, так что не уедет в git или на сервер.

## 4. Проверить

```bash
find ~/.claude/plugins -name fastpanel_accounts.py -exec python3 {} list \;
```

Должны напечататься имя аккаунта, адрес панели, логин и метка. Паролей эта команда не
печатает. Затем — проверка логина в панель:

```bash
find ~/.claude/plugins -name fastpanel_accounts.py -exec python3 {} whoami \;
```

Если доступы верные, увидишь свой user id на панели, домашний каталог, роли и квоту.

## 5. Пользоваться

Обычно просто говоришь Claude, что нужно: «покажи сайты на панели», «список баз»,
«заведи базу shop_main для сайта shop.example.com». Он сам выберет нужный скилл.

Напрямую, если надо:

```bash
P=$(find ~/.claude/plugins -type d -name fastpanel -path '*plugins/fastpanel')/skills

python3 $P/fastpanel-accounts/scripts/fastpanel_accounts.py list
python3 $P/fastpanel-sites/scripts/fastpanel_sites.py list
python3 $P/fastpanel-db/scripts/fastpanel_db.py servers
python3 $P/fastpanel-db/scripts/fastpanel_db.py add shop_main --site shop.example.com
```

Доступы к созданной базе кладутся в `~/.config/fastpanel/databases/<аккаунт>/<имя>.env`
(права `600`). Пароль базы в вывод не печатается намеренно — смотри файл сам.

## Обновление

```bash
cd ~/plugins/fastpanel-plugin && git pull      # или распакуй новый архив поверх
```

В Claude Code: `/plugin marketplace update fastpanel`.

## Удаление

```
/plugin uninstall fastpanel@fastpanel
/plugin marketplace remove fastpanel
```

Файл с доступами и выгруженные пароли баз остаются на месте — удали руками, если не нужны:
`rm -rf ~/.config/fastpanel ~/.cache/fastpanel`.

## Если что-то не работает

- `no panel config at ...` — не создан файл из шага 3; скрипт сам печатает готовые команды.
- `... is group/world accessible` — сделай `chmod 600 ~/.config/fastpanel/config.json`.
- `several accounts configured, pick one with --account` — в конфиге несколько аккаунтов;
  укажи `-A ИМЯ` или задай одному `"name": "default"`.
- `login failed ... (HTTP 401)` — неверный логин или пароль панели; проверь вход в браузере.
- `cannot reach https://...` — неверный хост или порт, либо панель недоступна снаружи.
