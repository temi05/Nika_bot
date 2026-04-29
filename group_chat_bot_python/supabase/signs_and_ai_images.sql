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
    option_id bigint,
    option_title text,
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

alter table public.sign_orders
add column if not exists option_id bigint;

alter table public.sign_orders
add column if not exists option_title text;

create table if not exists public.sign_price_options (
    id bigserial primary key,
    chat_id bigint not null,
    user_id bigint not null,
    title text not null,
    description text not null default '',
    price integer not null check (price > 0),
    is_active boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists idx_sign_price_options_user
on public.sign_price_options (chat_id, user_id, is_active);

create table if not exists public.bot_assets (
    asset_key text primary key,
    mime_type text not null,
    payload_base64 text not null,
    updated_at timestamptz not null default now(),
    updated_by bigint
);
