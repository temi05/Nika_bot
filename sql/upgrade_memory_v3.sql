ALTER TABLE public.bot_knowledge
  ALTER COLUMN meta SET DEFAULT '{}'::jsonb;

UPDATE public.bot_knowledge
SET meta = COALESCE(meta, '{}'::jsonb)
WHERE meta IS NULL;

UPDATE public.bot_knowledge
SET meta = jsonb_set(COALESCE(meta, '{}'::jsonb), '{memory_kind}', '"fact"'::jsonb, true)
WHERE COALESCE(meta->>'memory_kind', '') = '';

CREATE INDEX IF NOT EXISTS bot_knowledge_chat_memory_kind_idx
  ON public.bot_knowledge (chat_id, ((COALESCE(meta->>'memory_kind', 'fact'))), status, confidence DESC, last_seen_at DESC);
