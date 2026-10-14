-- ============================================================
-- ARGUS telemetry — its own Supabase project, nothing else lives here.
-- Cards come in only through the `telemetry-intake` edge function, which
-- writes with the service role. Nobody anonymous can read or write a row.
-- Admins (owner / maintainer) read through RLS. See docs/DATA.md.
-- ============================================================

-- ── Cards ─────────────────────────────────────────────────────
create table public.tele_cards (
  id                uuid        primary key default gen_random_uuid(),
  received_at       timestamptz not null default now(),
  install           text        not null,
  run               text        not null,
  fingerprint       text        not null,
  status            text        not null,
  argus_version     text        not null,
  langgraph_version text,
  python            text        not null,
  ci                boolean     not null,
  n_steps           integer     not null,
  n_findings        integer     not null,
  card              jsonb       not null,
  unique (install, run)
);

create index tele_cards_received on public.tele_cards (received_at desc);
create index tele_cards_fingerprint on public.tele_cards (install, fingerprint, received_at desc);
create index tele_cards_version on public.tele_cards (argus_version);

-- One row per finding, for the rule scoreboard.
create view public.tele_findings with (security_invoker = true) as
select
  c.id            as card_id,
  c.received_at,
  c.install,
  c.fingerprint,
  c.status        as run_status,
  c.argus_version,
  c.ci,
  f ->> 'type'     as rule,
  f ->> 'severity' as severity,
  f ->> 'source'   as source,
  f ->> 'node'     as node,
  f ->> 'origin'   as origin,
  (f ->> 'heuristic')::boolean  as heuristic,
  (f ->> 'suppressed')::boolean as suppressed
from public.tele_cards c,
     jsonb_array_elements(c.card -> 'findings') f;

-- ── Rate limits (per install and per hashed IP, per UTC day) ─────
create table public.tele_rate (
  bucket text    not null,  -- 'i:<install>' or 'ip:<sha256 of ip>'
  day    date    not null default (now() at time zone 'utc')::date,
  count  integer not null default 0,
  primary key (bucket, day)
);

-- Atomically count one request against every bucket; false when any is over.
create or replace function public.tele_take(buckets text[], cap integer)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
  over boolean := false;
  b text;
  n integer;
begin
  foreach b in array buckets loop
    insert into tele_rate (bucket, count) values (b, 1)
      on conflict (bucket, day) do update set count = tele_rate.count + 1
      returning count into n;
    if n > cap then over := true; end if;
  end loop;
  return not over;
end;
$$;

revoke all on function public.tele_take(text[], integer) from public, anon, authenticated;

-- ── Admins ───────────────────────────────────────────────────
create table public.admin_users (
  user_id  uuid        primary key references auth.users (id) on delete cascade,
  email    text        not null,
  role     text        not null check (role in ('owner', 'maintainer')),
  added_at timestamptz not null default now()
);

create unique index admin_users_one_owner on public.admin_users (role) where role = 'owner';

create or replace function public.admin_role()
returns text
language sql
stable
security definer
set search_path = public
as $$
  select role from admin_users where user_id = auth.uid()
$$;

-- RLS policies and the admin panel call it as a signed-in user; anon never needs it.
revoke all on function public.admin_role() from public, anon;
grant execute on function public.admin_role() to authenticated;

-- ── Row level security ───────────────────────────────────────
alter table public.tele_cards  enable row level security;
alter table public.tele_rate   enable row level security;
alter table public.admin_users enable row level security;

create policy "admins read cards" on public.tele_cards
  for select to authenticated using (public.admin_role() is not null);

create policy "owner deletes cards" on public.tele_cards
  for delete to authenticated using (public.admin_role() = 'owner');

create policy "admins read admins" on public.admin_users
  for select to authenticated using (public.admin_role() is not null);

create policy "owner adds maintainers" on public.admin_users
  for insert to authenticated
  with check (public.admin_role() = 'owner' and role = 'maintainer');

create policy "owner removes maintainers" on public.admin_users
  for delete to authenticated
  using (public.admin_role() = 'owner' and role = 'maintainer');

-- tele_rate: no policies — only the service role and tele_take() touch it.

-- ── Retention: 12 months ─────────────────────────────────────
create extension if not exists pg_cron;

select cron.schedule(
  'tele-retention',
  '17 3 * * *',
  $$
    delete from public.tele_cards where received_at < now() - interval '12 months';
    delete from public.tele_rate  where day < (now() at time zone 'utc')::date - 7;
  $$
);

-- ── Admin panel (admin/) ─────────────────────────────────────
-- Rule scoreboard: one row per rule. `ignored` = hits the user silenced with
-- `argus ignore` (the card's `suppressed` flag), the first false-alarm signal.
create view public.tele_rule_stats with (security_invoker = true) as
select
  rule,
  bool_or(heuristic)                         as heuristic,
  count(*)                                   as fires,
  count(distinct card_id)                    as runs,
  count(distinct install)                    as installs,
  count(*) filter (where suppressed)         as ignored,
  count(*) filter (where ci)                 as in_ci,
  count(*) filter (where severity = 'critical') as critical,
  max(received_at)                           as last_seen,
  array_agg(distinct argus_version)          as versions
from public.tele_findings
group by rule;

-- Daily volume for the overview chart.
create view public.tele_daily with (security_invoker = true) as
select
  (received_at at time zone 'utc')::date      as day,
  count(*)                                    as cards,
  count(distinct install)                     as installs,
  count(*) filter (where status <> 'clean')   as not_clean
from public.tele_cards
group by 1;

-- Overview: verdict mix per Argus version.
create view public.tele_status with (security_invoker = true) as
select status, argus_version, count(*) as cards, count(distinct install) as installs
from public.tele_cards
group by status, argus_version;

-- Owner adds a maintainer by email. They must have signed in once (that
-- creates their auth user); the owner cannot be replaced from the panel.
create or replace function public.add_maintainer(maintainer_email text)
returns text
language plpgsql
security definer
set search_path = public
as $$
declare
  uid uuid;
begin
  if public.admin_role() is distinct from 'owner' then
    raise exception 'only the owner can add maintainers';
  end if;
  select id into uid from auth.users where lower(email) = lower(trim(maintainer_email));
  if uid is null then
    return 'not_signed_in';
  end if;
  insert into admin_users (user_id, email, role)
    values (uid, lower(trim(maintainer_email)), 'maintainer')
    on conflict (user_id) do nothing;
  return 'added';
end;
$$;

create or replace function public.remove_maintainer(maintainer_id uuid)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  if public.admin_role() is distinct from 'owner' then
    raise exception 'only the owner can remove maintainers';
  end if;
  delete from admin_users where user_id = maintainer_id and role = 'maintainer';
end;
$$;

revoke all on function public.add_maintainer(text) from public, anon;
revoke all on function public.remove_maintainer(uuid) from public, anon;
grant execute on function public.add_maintainer(text) to authenticated;
grant execute on function public.remove_maintainer(uuid) to authenticated;

-- ── Bootstrap the owner (run once, by hand, after first sign-in) ─
-- insert into public.admin_users (user_id, email, role)
--   select id, email, 'owner' from auth.users where email = '<owner email>';
