# Python Bot Rewrite

Новая Python-версия бота лежит отдельно и не меняет текущий Node.js-бот.

## Что внутри

- `aiogram` 3 для Telegram
- `FastAPI` для webhook и health-check
- `Supabase` как основной backend
- AI-ответы через OpenAI-compatible API
- переключаемая память:
  - `MEMORY_PROVIDER=database`
  - `MEMORY_PROVIDER=lightrag`

## Структура

- `main.py` — точка входа для ASGI
- `app/web.py` — FastAPI + webhook + фоновые задачи
- `app/bot/` — команды, админка, сообщения
- `app/services/` — Supabase, AI, persona, memory

## Быстрый старт

1. Скопируй `.env.example` в `.env`
2. Заполни `TELEGRAM_BOT_TOKEN`, `SUPABASE_URL`, `SUPABASE_KEY`
3. При необходимости добавь `OPENAI_API_KEY`
4. Установи зависимости:

```bash
pip install -e .
```

5. Запусти:

```bash
uvicorn main:app --host 0.0.0.0 --port 8080
```

## Render

Для Render эта версия уже рассчитана на webhook-режим:

- сервис запускается командой `uvicorn main:app --host 0.0.0.0 --port $PORT`
- бот при старте пытается сам выставить webhook на `RENDER_EXTERNAL_URL`
- endpoint webhook: `/bot<TELEGRAM_BOT_TOKEN>`
- для защиты webhook используется `WEBHOOK_SECRET_TOKEN`
- health-check можно ставить на `/health`

Минимальный набор env:

```env
TELEGRAM_BOT_TOKEN=...
SUPABASE_URL=...
SUPABASE_KEY=...
RENDER_EXTERNAL_URL=https://your-service.onrender.com
WEBHOOK_SECRET_TOKEN=some-long-random-string
OPENAI_API_KEY=...
```

## Что уже перенесено

- `/help`
- `/me`
- `/top`
- `/daily`
- `/bio`
- `/mybirthday`
- `/notes`
- `/mood`
- `/linkfilter`
- `/shop`
- `/buy`
- `/give`
- `/kto`
- `/remind`
- `/ban`
- `/unban`
- `/banword`
- `/unbanword`
- `/listwords`
- XP за сообщения
- фильтр плохих слов
- фильтр `t.me` ссылок
- плюсики/спасибо/дизлайки в ответ на сообщения
- печеньки и магазин
- капча для новых участников
- фоновые напоминания
- поздравления с днём рождения
- AI-ответы по упоминанию или reply
- пассивная память
- `/health` и `/api/memory/health`

## Память

Сейчас память в Python-версии уже не пустая заглушка:

- последние сообщения буферизуются и пачками сохраняются как долговременный transcript-memory
- бот ищет релевантные факты по токенам запроса и по имени пользователя
- явные факты вроде `/bio` и `/mybirthday` теперь тоже сохраняются в память
- можно переключиться на `LightRAG`, если хочешь graph-enhanced память как в серверной схеме

То есть базовая память уже перенесена и даже стала практичнее для профиля пользователя.  
Но огромная кастомная JS-логика из старого `aiHandler.js` всё ещё упрощена: там было больше ручных режимов поведения и сложнее orchestration.

## Что сознательно не переносилось

- `miniapp/dashboard`
- связанный с miniapp API

## LightRAG

Для LightRAG:

```env
MEMORY_PROVIDER=lightrag
LIGHTRAG_BASE_URL=http://127.0.0.1:9621
LIGHTRAG_API_KEY=
LIGHTRAG_QUERY_MODE=hybrid
LIGHTRAG_WORKSPACE=telegram-bot
```
