---
name: fastpanel-sites
description: List the sites of a FastPanel/FluxPanel account and show one site in detail (id, aliases, document root, owner, SSL, ips). Use when asked what sites are on the panel, "какие сайты на панели", "покажи сайт example.com", or when a site id or document root is needed for another task.
---

# fastpanel-sites — сайты аккаунта панели

```bash
SCRIPT="${CLAUDE_PLUGIN_ROOT}/skills/fastpanel-sites/scripts/fastpanel_sites.py"

python3 "$SCRIPT" list [-A ИМЯ]            # домены и их site id
python3 "$SCRIPT" show DOMAIN [-A ИМЯ]     # подробности одного сайта
```

`list` печатает домен, `id` и пометки: `disabled`, `ssl`, `errors:N`.
`show` ищет домен и среди алиасов, печатает id, алиасы, корень сайта, владельца,
включён ли, есть ли сертификат, ip и дату создания.

Эндпоинты панели: `GET /api/sites/list?filter[...]` (постраничный) и `GET /api/sites/simple`
(короткий список id + домен, его же читает форма создания БД). `GET /api/sites` не существует.

Только чтение: этот инструмент ничего не меняет на панели.

Доступы и правила обращения с паролем: `${CLAUDE_PLUGIN_ROOT}/SETUP.md`. Коротко: один файл
`~/.config/fastpanel/config.json` (`600`), **читать его нельзя**, пароль в чат не попадает.
Если настроено несколько аккаунтов и не сказано, какой брать, — спроси.
