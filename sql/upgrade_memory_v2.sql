CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE public.bot_knowledge
  ADD COLUMN IF NOT EXISTS fact_type text DEFAULT 'fact',
  ADD COLUMN IF NOT EXISTS subject_name text,
  ADD COLUMN IF NOT EXISTS relation_type text,
  ADD COLUMN IF NOT EXISTS object_name text,
  ADD COLUMN IF NOT EXISTS confidence double precision DEFAULT 0.55,
  ADD COLUMN IF NOT EXISTS status text DEFAULT 'confirmed',
  ADD COLUMN IF NOT EXISTS source_count integer DEFAULT 1,
  ADD COLUMN IF NOT EXISTS times_seen integer DEFAULT 1,
  ADD COLUMN IF NOT EXISTS fingerprint text,
  ADD COLUMN IF NOT EXISTS meta jsonb DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS last_seen_at timestamp with time zone DEFAULT timezone('utc'::text, now());

UPDATE public.bot_knowledge
SET
  fact_type = COALESCE(fact_type, 'fact'),
  confidence = COALESCE(confidence, 0.7),
  status = COALESCE(status, 'confirmed'),
  source_count = COALESCE(source_count, 1),
  times_seen = COALESCE(times_seen, 1),
  meta = COALESCE(meta, '{}'::jsonb),
  last_seen_at = COALESCE(last_seen_at, created_at)
WHERE
  fact_type IS NULL
  OR confidence IS NULL
  OR status IS NULL
  OR source_count IS NULL
  OR times_seen IS NULL
  OR meta IS NULL
  OR last_seen_at IS NULL;

CREATE INDEX IF NOT EXISTS bot_knowledge_chat_status_idx
  ON public.bot_knowledge (chat_id, status, confidence DESC, last_seen_at DESC);

CREATE INDEX IF NOT EXISTS bot_knowledge_chat_subject_idx
  ON public.bot_knowledge (chat_id, subject_name);

CREATE INDEX IF NOT EXISTS bot_knowledge_chat_fingerprint_idx
  ON public.bot_knowledge (chat_id, fingerprint);

CREATE TABLE IF NOT EXISTS public.bot_memory_summaries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  chat_id bigint NOT NULL,
  period_key text NOT NULL,
  summary text NOT NULL,
  source_count integer DEFAULT 1,
  created_at timestamp with time zone DEFAULT timezone('utc'::text, now()) NOT NULL,
  updated_at timestamp with time zone DEFAULT timezone('utc'::text, now()) NOT NULL,
  UNIQUE(chat_id, period_key)
);

CREATE INDEX IF NOT EXISTS bot_memory_summaries_chat_period_idx
  ON public.bot_memory_summaries (chat_id, period_key);

CREATE OR REPLACE FUNCTION public.touch_bot_memory_summary(
  p_chat_id bigint,
  p_period_key text,
  p_summary text,
  p_source_inc integer DEFAULT 1
)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
  INSERT INTO public.bot_memory_summaries (chat_id, period_key, summary, source_count)
  VALUES (p_chat_id, p_period_key, p_summary, GREATEST(p_source_inc, 1))
  ON CONFLICT (chat_id, period_key)
  DO UPDATE SET
    summary = EXCLUDED.summary,
    source_count = public.bot_memory_summaries.source_count + GREATEST(p_source_inc, 1),
    updated_at = timezone('utc'::text, now());
END;
$$;

CREATE OR REPLACE FUNCTION public.match_knowledge_v2(
  query_embedding vector(1536),
  match_threshold double precision,
  match_count integer,
  p_chat_id bigint,
  p_statuses text[] DEFAULT ARRAY['confirmed'],
  p_min_confidence double precision DEFAULT 0.55
)
RETURNS TABLE (
  id uuid,
  fact text,
  similarity double precision,
  confidence double precision,
  status text,
  times_seen integer,
  source_count integer,
  subject_name text,
  fact_type text,
  last_seen_at timestamp with time zone
)
LANGUAGE sql
STABLE
AS $$
  SELECT
    bk.id,
    bk.fact,
    1 - (bk.embedding <=> query_embedding) AS similarity,
    bk.confidence,
    bk.status,
    bk.times_seen,
    bk.source_count,
    bk.subject_name,
    bk.fact_type,
    bk.last_seen_at
  FROM public.bot_knowledge bk
  WHERE bk.chat_id = p_chat_id
    AND bk.embedding IS NOT NULL
    AND bk.status = ANY(p_statuses)
    AND COALESCE(bk.confidence, 0) >= p_min_confidence
    AND 1 - (bk.embedding <=> query_embedding) > match_threshold
  ORDER BY bk.embedding <=> query_embedding
  LIMIT match_count;
$$;
