-- ─────────────────────────────────────────────────────────────
--  VidStream Pro — Supabase Database Schema
--  Paste this entire file into:
--  Supabase Dashboard → SQL Editor → New query → Run
-- ─────────────────────────────────────────────────────────────

-- 1. UUID extension
create extension if not exists "uuid-ossp";

-- 2. Profiles table (one row per user)
create table if not exists public.profiles (
  id                 uuid references auth.users(id) on delete cascade primary key,
  name               text,
  plan               text not null default 'free' check (plan in ('free','pro')),
  stripe_customer_id text,
  created_at         timestamptz default now(),
  updated_at         timestamptz default now()
);

-- 3. Auto-create profile row on every new sign-up
create or replace function public.handle_new_user()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  insert into public.profiles (id, name)
  values (
    new.id,
    coalesce(
      new.raw_user_meta_data->>'full_name',
      split_part(new.email, '@', 1)
    )
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute procedure public.handle_new_user();

-- 4. Download logs
create table if not exists public.download_logs (
  id         uuid        default uuid_generate_v4() primary key,
  user_id    uuid        references auth.users(id) on delete cascade not null,
  url        text,
  title      text,
  platform   text,
  quality    text,
  format     text        default 'mp4',
  thumbnail  text,
  created_at timestamptz default now()
);

create index if not exists dl_logs_user_idx on public.download_logs(user_id);
create index if not exists dl_logs_time_idx on public.download_logs(created_at desc);

-- 5. Row Level Security
alter table public.profiles      enable row level security;
alter table public.download_logs enable row level security;

-- Profiles policies
create policy "Own profile select"
  on public.profiles for select using (auth.uid() = id);

create policy "Own profile update"
  on public.profiles for update using (auth.uid() = id);

create policy "Service role all on profiles"
  on public.profiles for all using (auth.role() = 'service_role');

-- Download logs policies
create policy "Own logs select"
  on public.download_logs for select using (auth.uid() = user_id);

create policy "Own logs insert"
  on public.download_logs for insert with check (auth.uid() = user_id);

create policy "Own logs delete"
  on public.download_logs for delete using (auth.uid() = user_id);

create policy "Service role all on download_logs"
  on public.download_logs for all using (auth.role() = 'service_role');
