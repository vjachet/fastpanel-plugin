---
name: fastpanel-accounts
description: List the FastPanel/FluxPanel accounts configured on this machine and show who a given account is on the panel (user id, home dir, owner, quota). Use when asked which panels or panel users are set up, "какие аккаунты панели настроены", "кто я на панели", or when another fastpanel tool needs an account to be picked.
---

# fastpanel-accounts — какие аккаунты панели настроены

```bash
SCRIPT="${CLAUDE_PLUGIN_ROOT}/skills/fastpanel-accounts/scripts/fastpanel_accounts.py"

python3 "$SCRIPT" list                  # настроенные аккаунты: имя, панель, логин, метка
python3 "$SCRIPT" whoami [-A ИМЯ]       # логин в панель и кто мы там
```

`list` работает без сети и без пароля — только читает конфиг. `whoami` логинится в панель
и печатает имя аккаунта, адрес панели, логин, числовой user id, домашний каталог, роли,
владельца аккаунта и квоту.

Доступы, формат конфига и правила обращения с паролем: `${CLAUDE_PLUGIN_ROOT}/SETUP.md`.
Коротко: один файл `~/.config/fastpanel/config.json` (`600`), **читать его нельзя**
(`cat`/`grep`/`head` по `~/.config/fastpanel/` запрещены), пароль в чат не попадает.
Если файла нет — скрипт печатает инструкцию по настройке, покажи её пользователю целиком.

Если настроено несколько аккаунтов и пользователь не сказал, какой брать, — покажи вывод
`list` и спроси. Молча не выбирай.
