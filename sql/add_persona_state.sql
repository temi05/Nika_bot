CREATE TABLE IF NOT EXISTS public.bot_persona_state (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  chat_id bigint NOT NULL,
  user_id bigint NOT NULL,
  troll double precision DEFAULT 0.42 NOT NULL,
  warmth double precision DEFAULT 0.58 NOT NULL,
  chaos double precision DEFAULT 0.32 NOT NULL,
  attachment double precision DEFAULT 0.18 NOT NULL,
  stage text DEFAULT 'fresh' NOT NULL,
  exchanges integer DEFAULT 0 NOT NULL,
  updated_at timestamp with time zone DEFAULT timezone('utc'::text, now()) NOT NULL,
  UNIQUE(chat_id, user_id)
);

CREATE INDEX IF NOT EXISTS bot_persona_state_chat_idx
  ON public.bot_persona_state (chat_id, user_id, updated_at DESC);
