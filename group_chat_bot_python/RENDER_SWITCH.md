# Render Switch Guide

## Как заменить старый Node.js бот на Python

Если у тебя сейчас один Render service `Nika_bot`, ты можешь перевести его на Python-версию без создания нового имени сервиса.

### Вариант 1. Самый простой

Открываешь настройки текущего сервиса `Nika_bot` и меняешь:

- `Root Directory` -> `group_chat_bot_python`
- `Build Command`:

```bash
pip install -e .
```

- `Start Command`:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

### Какие env должны быть у Python-бота

```env
TELEGRAM_BOT_TOKEN=...
SUPABASE_URL=...
SUPABASE_KEY=...
OPENAI_API_KEY=...

RENDER_EXTERNAL_URL=https://твой-сервис.onrender.com
WEBHOOK_SECRET_TOKEN=длинная_случайная_строка

MEMORY_PROVIDER=chroma
MEMORY_BACKUP_CHAT_ID=-100...

SUPERADMIN_IDS=861713427
DEVELOPER_USERNAMES=

KEEP_ALIVE_ENABLED=true
KEEP_ALIVE_INTERVAL_SECONDS=600
```

## Как сделать, чтобы бот НЕ засыпал на Render (Free план)

На бесплатном тарифе Render усыпляет Web Service через **15 минут без входящих внешних HTTP-запросов**. В боте уже встроены:

1. Мгновенные легковесные эндпоинты `GET /ping`, `HEAD /ping` и `GET /healthz` (отвечают за `<2ms`, не нагружая ИИ и базу).
2. Внутренний фоновый `keep_alive_loop` (раз в `KEEP_ALIVE_INTERVAL_SECONDS`, по умолчанию `600` сек = 10 мин), который пингует внешний `RENDER_EXTERNAL_URL/ping` и поддерживает активность соединения с Supabase.
3. Готовый GitHub Actions workflow `.github/workflows/keep_render_awake.yml` (каждые 10 минут будит/удерживает Render снаружи).

### Чтобы включить внешний анти-сон на 100%:

- **Способ А (GitHub Actions — уже в репозитории, бесплатно):**
  Открой на GitHub: `Settings` -> `Secrets and variables` -> `Actions` -> вкладка `Variables` -> `New repository variable`:
  - Name: `RENDER_EXTERNAL_URL`
  - Value: `https://твой-сервис.onrender.com`
- **Способ Б (`cron-job.org` или `UptimeRobot` — для точного интервала каждые 5–10 минут, бесплатно):**
  Создай HTTP-монитор на адрес `https://твой-сервис.onrender.com/ping` с интервалом **5 или 10 минут** (метод `GET` или `HEAD`). Это гарантирует, что контейнер на Render вообще никогда не уйдёт в `spin down`.

## Доступ разработчика без админки в чате

- Укажи свой Telegram ID в `SUPERADMIN_IDS` (или username без `@` в `DEVELOPER_USERNAMES`).
- Команда `/whoami` покажет твой ID, статус разработчика и права самого бота в текущей группе.
- Команда `/status` покажет аптайм инстанса, число фактов в ChromaDB, состояние Supabase и настройки чата.
- **Важное ограничение Telegram Bot API:** ты можешь вызывать любые команды бота без админки у себя, но для физического кика/бана/мута участников или удаления чужих сообщений **самому боту** всё равно нужны права администратора группы в Telegram.

## Что будет со старым Node кодом

Ничего удалять не надо — Render запускает `group_chat_bot_python`, если указан `Root Directory = group_chat_bot_python`.
