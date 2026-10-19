-- ============================================================
-- ARGUS telemetry: per-person admin permissions. Applied after migration.sql,
-- which stays as it was applied.
--
-- The owner holds every permission. Each maintainer holds the ones the owner
-- ticks, out of four:
--   overview      daily volume and verdicts by version (tele_daily, tele_status)
--   rules         the rule scoreboard (tele_rule_stats)
--   runs          the cards themselves (tele_cards, tele_findings)
--   delete_cards  delete a card; works together with runs, because a delete
--                 has to see the row first
-- Managing the team stays with the owner and cannot be granted.
--
-- Maintainers who exist when this runs get overview, rules and runs: what
-- every maintainer could read before, so nobody loses access.
-- ============================================================

alter table public.admin_users
  add column permissions text[] not null default '{overview,rules,runs}'
  constraint admin_users_permissions_known
    check (permissions <@ array['overview', 'rules', 'runs', 'delete_cards']);

-- True when the signed-in admin holds `p`. The owner holds everything.
create or replace function public.has_perm(p text)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select coalesce(
    (select a.role = 'owner' or p = any (a.permissions)
       from public.admin_users a
      where a.user_id = (select auth.uid())),
    false)
$$;

-- ── Cards: reading needs runs, deleting needs delete_cards ─────
drop policy "admins read cards" on public.tele_cards;
drop policy "owner deletes cards" on public.tele_cards;

create policy "runs readers read cards" on public.tele_cards
  for select to authenticated using ((select public.has_perm('runs')));

create policy "card deleters delete cards" on public.tele_cards
  for delete to authenticated using ((select public.has_perm('delete_cards')));

-- ── Overview and rules: their own permission, not runs ─────────
-- The views read tele_cards as the caller (security_invoker), so through
-- them the overview would need runs. Each view is read instead through a
-- function that checks its own permission and reads the cards as the
-- owner of the table. The views themselves are closed to the API roles.
revoke all on public.tele_daily, public.tele_status, public.tele_rule_stats from anon, authenticated;

create or replace function public.tele_daily_rows()
returns setof public.tele_daily
language sql
stable
security definer
set search_path = ''
as $$
  select * from public.tele_daily where (select public.has_perm('overview'))
$$;

create or replace function public.tele_status_rows()
returns setof public.tele_status
language sql
stable
security definer
set search_path = ''
as $$
  select * from public.tele_status where (select public.has_perm('overview'))
$$;

create or replace function public.tele_rule_stats_rows()
returns setof public.tele_rule_stats
language sql
stable
security definer
set search_path = ''
as $$
  select * from public.tele_rule_stats where (select public.has_perm('rules'))
$$;

-- ── The owner sets a maintainer's permissions ─────────────────
-- The column constraint above refuses anything but the four names.
create or replace function public.set_admin_permissions(user_id uuid, perms text[])
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
  if public.admin_role() is distinct from 'owner' then
    raise exception 'only the owner can set permissions';
  end if;
  update public.admin_users a
     set permissions = perms
   where a.user_id = set_admin_permissions.user_id
     and a.role = 'maintainer';
  if not found then
    raise exception 'no contributor with that id: the owner holds every permission';
  end if;
end;
$$;

revoke all on function
  public.has_perm(text),
  public.tele_daily_rows(),
  public.tele_status_rows(),
  public.tele_rule_stats_rows(),
  public.set_admin_permissions(uuid, text[])
  from public, anon;
grant execute on function
  public.has_perm(text),
  public.tele_daily_rows(),
  public.tele_status_rows(),
  public.tele_rule_stats_rows(),
  public.set_admin_permissions(uuid, text[])
  to authenticated;
