alter table public.users
add column if not exists sign_price integer not null default 0;

create table if not exists public.sign_orders (
    id bigserial primary key,
    chat_id bigint not null,
    buyer_id bigint not null,
    buyer_name text not null default '',
    author_id bigint not null,
    author_name text not null default '',
    price integer not null check (price > 0),
    escrow_amount integer not null default 0,
    text text not null default '',
    status text not null default 'pending',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    accepted_at timestamptz,
    delivered_at timestamptz,
    paid_at timestamptz,
    cancelled_at timestamptz,
    cancel_reason text
);

create index if not exists idx_sign_orders_chat_author
on public.sign_orders (chat_id, author_id, status);

create index if not exists idx_sign_orders_chat_buyer
on public.sign_orders (chat_id, buyer_id, status);

create table if not exists public.bot_assets (
    asset_key text primary key,
    mime_type text not null,
    payload_base64 text not null,
    updated_at timestamptz not null default now(),
    updated_by bigint
);
