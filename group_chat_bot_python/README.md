# Python Bot Rewrite

Python-версия бота живет отдельно от старого Node.js-бота и рассчитана на webhook-режим в Render.

## Что уже есть

- `aiogram` 3 + `FastAPI`
- webhook на `/bot<TELEGRAM_BOT_TOKEN>`
- автопопытка `setWebhook` при старте
- `Supabase` как база
- память через `database` или `LightRAG`
- команды `/me`, `/top`, `/daily`, `/bio`, `/mybirthday`, `/notes`, `/mood`, `/linkfilter`, `/shop`, `/buy`, `/give`, `/kto`, `/remind`
- админ-команды `/ban`, `/unban`, `/banword`, `/unbanword`, `/listwords`
- XP, печеньки, варны, фильтры, напоминания, капча, поздравления с днем рождения
- AI-ответы с character prompt и tool-use

## AI tools

Сейчас в Python-версии AI умеет вызывать инструменты для:

- поиска профиля и поиска людей по описанию
- обновления био
- сохранения AI-заметок о пользователях
- награды печеньками
- варна
- мута и размута

Это уже ближе к старому `aiHandler.js`, но реализация чище и компактнее.

## Запуск локально

```bash
pip install -e .
uvicorn main:app --host 0.0.0.0 --port 8080
```

## Render

Start Command:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Минимальные env:

```env
TELEGRAM_BOT_TOKEN=...
SUPABASE_URL=...
SUPABASE_KEY=...
RENDER_EXTERNAL_URL=https://your-service.onrender.com
WEBHOOK_SECRET_TOKEN=some-long-random-string
OPENAI_API_KEY=...
```

## LightRAG

Для внешнего LightRAG:

```env
MEMORY_PROVIDER=lightrag
LIGHTRAG_BASE_URL=https://your-lightrag-service.onrender.com
LIGHTRAG_QUERY_MODE=hybrid
LIGHTRAG_WORKSPACE=telegram-bot
```
