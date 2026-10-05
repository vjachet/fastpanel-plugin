---
name: fastpanel-accounts
description: List the FastPanel/FluxPanel accounts configured on this machine and show who a given account is on the panel (user id, home dir, owner id, quota). Use when asked which panels or panel users are set up, "какие аккаунты панели настроены", "кто я на панели", or when another fastpanel tool needs an account to be picked.
---

# fastpanel-accounts — какие аккаунты панели настроены

```bash
SCRIPT="${CLAUDE_PLUGIN_ROOT}/skills/fastpanel-accounts/scripts/fastpanel_accounts.py"

python3 "$SCRIPT" list                  # настроенные аккаунты: имя, панель, метка
python3 "$SCRIPT" whoami [-A ИМЯ]       # вход в панель и кто мы там
python3 "$SCRIPT" users [-A ИМЯ]        # пользователи панели, которых видит аккаунт: id и логин
```

`list` работает без сети и без пароля — только читает конфиг. `whoami` логинится в панель
и печатает имя аккаунта, адрес панели, числовой user id, домашний каталог, роли,
id владельца аккаунта и квоту. Логин панели не печатается нигде.

Доступы, формат конфига и правила обращения с логином, паролем и токеном:
`${CLAUDE_PLUGIN_ROOT}/SETUP.md`. Коротко: один файл `~/.config/fastpanel/config.json`
(`600`), **читать его и кеш токена `~/.cache/fastpanel/` нельзя**; логин, пароль и токен в
чат не попадают — в выводе скриптов они заменены на `***`.
Если файла нет — скрипт печатает инструкцию по настройке, покажи её пользователю целиком.

`users` нужен, когда аккаунт видит несколько пользователей панели (администратор `fastpanel`
видит всех): при создании сайта или базы тогда обязателен `--owner`. Покажи список
пользователю и спроси, от чьего имени создавать; сам не выбирай. От имени `fastpanel` ничего
не создаётся. Свои (настроенные) пользователи показаны как `account ИМЯ` — так их и
передавай в `--owner`.

Если настроено несколько аккаунтов и пользователь не сказал, какой брать, — покажи вывод
`list` и спроси. Молча не выбирай.
