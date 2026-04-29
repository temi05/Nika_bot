# NeuroNika Python Bot

Python-версия бота. FastAPI + aiogram 3, webhook-режим, Supabase как единственная база.

## Что есть

- `aiogram` 3 + `FastAPI`, webhook на `/bot<TELEGRAM_BOT_TOKEN>`
- автопопытка `setWebhook` при старте
- `Supabase` — пользователи, память, настройки, заметки, напоминания
- AI-ответы с character prompt, tool-use и персонификацией по пользователям
- AI-извлечение памяти: диалог режется в summary + факты
- опросы, профиль, заметки, печеньки, варны, мут и размут через tools

## Prompt Architecture

- `app/services/prompt_builders.py` — character/system prompt и memory extraction prompt  
- `app/services/ai_service.py` — orchestration, tools, direct poll handling, logging  
- `app/services/memory_provider.py` — извлечение и сборка памяти в Supabase  

Режим характера задаётся через env:

```env
BOT_PERSONALITY_MODE=hard
```

Режимы:
- `normal` — дерзкая, но относительно аккуратная
- `hard` — уверенная, колкая, с нажимом (по умолчанию)
- `insane` — максимально наглая и доминирующая версия

## Memory Tuning

```env
AI_MODEL=gpt-4o-mini
AI_FALLBACK_MODEL=gpt-4o-mini
AI_IMAGE_MODEL=gpt-image-1.5
MEMORY_MODEL=
MEMORY_EXTRACTION_ENABLED=true
MEMORY_EXTRACTION_MAX_FACTS=6
MEMORY_FACT_MIN_CONFIDENCE=0.72
MEMORY_RETRIEVAL_LIMIT=6
MEMORY_CAPTURE_ALL_MESSAGES=false
```

- `AI_MODEL` — диалог, самый совместимый вариант для текущего `chat.completions`-кода: `gpt-4o-mini`
- `AI_FALLBACK_MODEL` — запасная модель, если основной ответ пришел пустым
- `AI_IMAGE_MODEL` — генерация ИИ-картинок и ИИ-сигн, рекомендуемо `gpt-image-1.5`
- `MEMORY_MODEL` — можно задать отдельный более дешевый/точный слой
- `MEMORY_FACT_MIN_CONFIDENCE` — фильтрует мусорные факты
- `MEMORY_RETRIEVAL_LIMIT` — сколько фактов тащить в prompt
- `MEMORY_CAPTURE_ALL_MESSAGES=false` — память по ответам бота и по сообщениям, похожим на важные факты; `true` сохраняет намного больше

## Cost Tuning

```env
AI_MAX_TOKENS=220
AI_HISTORY_LINES=4
AI_COMPACT_PROMPT=true
AI_GROUP_COOLDOWN_SECONDS=12
AI_MIN_MESSAGE_LEN=4
AI_IMAGE_PRICE=650
AI_SIGN_PRICE=450
HUMAN_SIGN_MIN_PRICE=300
```

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

Health check: `/health`

Minimal env:

```env
TELEGRAM_BOT_TOKEN=...
SUPABASE_URL=...
SUPABASE_KEY=...
RENDER_EXTERNAL_URL=https://your-service.onrender.com
WEBHOOK_SECRET_TOKEN=some-long-random-string
OPENAI_API_KEY=...
```

## Supabase tables

Необходимые таблицы создаются SQL-скриптами в папке `supabase/`:
- `supabase/message_logs.sql`
- `supabase/bot_persona_state.sql`
- `supabase/signs_and_ai_images.sql`
