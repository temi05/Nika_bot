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

MEMORY_PROVIDER=database
```

Если подключаешь `LightRAG`, тогда:

```env
MEMORY_PROVIDER=lightrag
LIGHTRAG_BASE_URL=https://твой-lightrag.onrender.com
LIGHTRAG_QUERY_MODE=hybrid
LIGHTRAG_WORKSPACE=telegram-bot
```

## Что будет со старым Node кодом

Ничего удалять не надо.

Render просто перестанет запускать папку `group_chat_bot` и начнёт запускать `group_chat_bot_python`, если ты поменяешь `Root Directory` и команды сборки/старта.

То есть:

- старый Node-код останется в репозитории
- новый Python-код станет прод-версией

## Что я советую

1. Сначала перевести `Nika_bot` на Python с `MEMORY_PROVIDER=database`
2. Проверить, что бот отвечает и webhook работает
3. Потом создать второй сервис под `LightRAG`
4. Только после этого переключить `MEMORY_PROVIDER=lightrag`
