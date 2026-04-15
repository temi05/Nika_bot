create table if not exists public.memory_sync_queue (
    id bigserial primary key,
    chat_id bigint not null,
    transcript text not null,
    participants jsonb not null default '[]'::jsonb,
    provider text not null default 'lightrag',
    workspace text null,
    status text not null default 'pending',
    attempts integer not null default 0,
    next_attempt_at timestamptz not null default timezone('utc', now()),
    last_error text null,
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now())
);

create index if not exists memory_sync_queue_provider_status_idx
    on public.memory_sync_queue (provider, status, next_attempt_at);

create index if not exists memory_sync_queue_chat_id_idx
    on public.memory_sync_queue (chat_id, created_at desc);
