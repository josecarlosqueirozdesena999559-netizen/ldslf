create table if not exists public.bot_settings (
  id text primary key default 'default',
  data jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

create table if not exists public.session_score (
  id text primary key default 'default',
  wins integer not null default 0,
  losses integer not null default 0,
  profit numeric not null default 0,
  results jsonb not null default '[]'::jsonb,
  last_green_time text not null default '-',
  updated_at timestamptz not null default now()
);

create table if not exists public.manual_entries (
  id text primary key,
  status text not null default 'AGUARDANDO',
  data jsonb not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.trade_history (
  id bigserial primary key,
  asset text not null,
  direction text not null,
  result text not null,
  profit numeric not null default 0,
  data jsonb not null,
  created_at timestamptz not null default now()
);

create index if not exists idx_manual_entries_status on public.manual_entries (status);
create index if not exists idx_trade_history_created_at on public.trade_history (created_at desc);
create index if not exists idx_trade_history_asset on public.trade_history (asset);
create index if not exists idx_trade_history_result on public.trade_history (result);
