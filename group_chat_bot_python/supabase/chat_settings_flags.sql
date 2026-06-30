create table if not exists public.chats (
    chat_id bigint primary key,
    link_filter_enabled boolean not null default true,
    casino_jackpot integer not null default 0,
    auto_drop_enabled boolean not null default true,
    auto_quiz_enabled boolean not null default true,
    ai_enabled boolean not null default true,
    proactive_enabled boolean not null default true
);

alter table public.chats
    add column if not exists link_filter_enabled boolean not null default true,
    add column if not exists casino_jackpot integer not null default 0,
    add column if not exists auto_drop_enabled boolean not null default true,
    add column if not exists auto_quiz_enabled boolean not null default true,
    add column if not exists ai_enabled boolean not null default true,
    add column if not exists proactive_enabled boolean not null default true;