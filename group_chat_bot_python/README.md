# Python Bot Rewrite

Python-версия бота живет отдельно от старого Node.js-бота и рассчитана на webhook-режим через FastAPI + aiogram.

## Что уже есть

- `aiogram` 3 + `FastAPI`
- webhook на `/bot<TELEGRAM_BOT_TOKEN>`
- автопопытка `setWebhook` при старте
- `Supabase` как база
- память через `database` или `LightRAG`
- AI-ответы с character prompt, tool-use и персонификацией по пользователям
- AI memory extraction: диалог режется в summary + факты, а не просто валится сырым логом
- опросы, профиль, заметки, печеньки, варны, мут и размут через tools

## Prompt Architecture

Промпты в этом проекте лучше писать не на "языке программирования", а как структурированные текстовые инструкции.

Практический вариант для этого репозитория:

- сам prompt писать обычным естественным языком;
- для характера и чата использовать русский, потому что сам бот живет в русском Telegram-контексте;
- Python использовать только как слой сборки prompt-шаблонов и подстановки runtime-контекста.

Режим характера теперь можно менять через env:

```env
BOT_PERSONALITY_MODE=hard
```

Режимы:

- `normal` — дерзкая, но относительно аккуратная
- `hard` — уверенная, колкая, с уместным матом и нажимом
- `insane` — максимально наглая, жёсткая и доминирующая версия

Где это лежит сейчас:

- `app/services/prompt_builders.py` — character/system prompt и memory extraction prompt
- `app/services/ai_service.py` — orchestration, tools, direct poll handling, logging
- `app/services/memory_provider.py` — извлечение и сборка памяти

## Memory Tuning

Новые env-поля для качества памяти:

```env
MEMORY_MODEL=
MEMORY_EXTRACTION_ENABLED=true
MEMORY_EXTRACTION_MAX_FACTS=6
MEMORY_FACT_MIN_CONFIDENCE=0.72
MEMORY_RETRIEVAL_LIMIT=6
```

Идея такая:

- `AI_MODEL` отвечает за диалог
- `MEMORY_MODEL` можно задать отдельно, если хочешь вынести память на другой более дешевый или более точный модельный слой
- `MEMORY_FACT_MIN_CONFIDENCE` режет мусорные факты
- `MEMORY_RETRIEVAL_LIMIT` контролирует, сколько тематической памяти бот тащит в prompt

## Run Local

```bash
pip install -e .
uvicorn main:app --host 0.0.0.0 --port 8080
```

## Render

Start command:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Recommended health check:

```text
/health
```

Minimal env:

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
MEMORY_SYNC_BATCH_SIZE=5
MEMORY_SYNC_RETRY_BASE_SECONDS=30
MEMORY_SYNC_RETRY_MAX_SECONDS=1800
MEMORY_SYNC_MAX_ATTEMPTS=20
```

### Durable LightRAG Queue

Если `LightRAG` спит или временно отдаёт `502`, память теперь не теряется:

- transcript сначала пишется в `Supabase` как job в очереди;
- бот пробует отправить его сразу;
- если upstream недоступен, job остаётся `pending` и ретраится в фоне;
- после успеха job помечается как `done`, после слишком многих неудач — как `failed`.

Перед включением `MEMORY_PROVIDER=lightrag` создай таблицу очереди в Supabase:

```sql
-- файл: supabase/memory_sync_queue.sql
```

Для постоянного учёта реакций и авторов сообщений также нужна таблица:

```sql
-- файл: supabase/message_logs.sql
```

Для persona-state и новых полей отношений выполни:

```sql
-- файл: supabase/bot_persona_state.sql
```
