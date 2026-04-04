-- Включаем расширение для работы с векторами
CREATE EXTENSION IF NOT EXISTS vector;

-- Создаем таблицу для хранения фактов (Досье)
CREATE TABLE IF NOT EXISTS bot_knowledge (
  id uuid primary key default gen_random_uuid(),
  chat_id bigint not null,
  user_id bigint, -- может быть null, если факт относится ко всему чату
  fact text not null,
  embedding vector(1536), -- размерность модели text-embedding-3-small
  created_at timestamp with time zone default timezone('utc'::text, now()) not null
);

-- Индекс для быстрого векторного поиска (опционально, но полезно при росте базы)
CREATE INDEX ON bot_knowledge USING hnsw (embedding vector_l2_ops);

-- Функция для поиска релевантных фактов по косинусному сходству
CREATE OR REPLACE FUNCTION match_knowledge (
  query_embedding vector(1536),
  match_threshold float,
  match_count int,
  p_chat_id bigint
)
RETURNS TABLE (
  id uuid,
  fact text,
  similarity float
)
LANGUAGE sql STABLE
AS $$
  SELECT
    bot_knowledge.id,
    bot_knowledge.fact,
    1 - (bot_knowledge.embedding <=> query_embedding) AS similarity
  FROM bot_knowledge
  WHERE bot_knowledge.chat_id = p_chat_id
    AND 1 - (bot_knowledge.embedding <=> query_embedding) > match_threshold
  ORDER BY bot_knowledge.embedding <=> query_embedding
  LIMIT match_count;
$$;
