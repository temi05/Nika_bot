alter table public.users
  add column if not exists jailed_until timestamptz,
  add column if not exists jail_reason text,
  add column if not exists steal_fail_streak integer not null default 0,
  add column if not exists steal_success_streak integer not null default 0;

create table if not exists public.bot_debts (
  id bigserial primary key,
  chat_id bigint not null,
  lender_id bigint not null,
  lender_name text not null default '',
  borrower_id bigint not null,
  borrower_name text not null default '',
  amount integer not null check (amount > 0),
  paid_amount integer not null default 0 check (paid_amount >= 0),
  forgiven_amount integer not null default 0 check (forgiven_amount >= 0),
  status text not null default 'active' check (status in ('active', 'repaid', 'forgiven')),
  created_at timestamptz not null default now(),
  due_at timestamptz not null default (now() + interval '48 hours'),
  repaid_at timestamptz,
  forgiven_at timestamptz,
  check (paid_amount + forgiven_amount <= amount)
);

create index if not exists bot_debts_borrower_active_idx
  on public.bot_debts (chat_id, borrower_id, status, created_at);

create index if not exists bot_debts_lender_active_idx
  on public.bot_debts (chat_id, lender_id, status, created_at);

create index if not exists users_jailed_until_idx
  on public.users (chat_id, jailed_until);
