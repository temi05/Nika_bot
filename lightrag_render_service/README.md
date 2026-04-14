# LightRAG Render Service

Это минимальная папка-шаблон для отдельного сервиса `LightRAG` на Render.

## Что здесь есть

- `requirements.txt` — ставит `lightrag-hku[api]`
- `.env.example` — минимальные env-переменные

## Как создать сервис на Render

Создай новый `Web Service` и укажи:

- `Root Directory`: `lightrag_render_service`
- `Build Command`:

```bash
pip install -r requirements.txt
```

- `Start Command`:

```bash
lightrag-server
```

## Какие env нужны

Минимально:

```env
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
EMBEDDING_MODEL=text-embedding-3-large
RAG_DIR=/opt/render/project/src/lightrag_data
```

После деплоя Render даст URL вида:

```text
https://nika-lightrag.onrender.com
```

Его потом нужно вставить в Python-бот:

```env
MEMORY_PROVIDER=lightrag
LIGHTRAG_BASE_URL=https://nika-lightrag.onrender.com
LIGHTRAG_QUERY_MODE=hybrid
LIGHTRAG_WORKSPACE=telegram-bot
```

## Важно

`LightRAG` лучше держать отдельным сервисом, а не внутри того же web service, где бот.
