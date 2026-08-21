-- Daily FX rate history, used to convert stored USD/AUD/CAD/GBP values into
-- a user's chosen display currency correctly: today's rate for current
-- totals, the rate that was actually in effect on a given date for
-- historical chart points. Replaces the static FX_USD_TO_* env vars as the
-- source of truth once populated -- those remain as a last-resort fallback
-- for dates this table has no row for yet (e.g. before backfill ran).
--
-- Convention matches app/services/pricing/currency_conversion.py's
-- existing _usd_rate(): usd_rate = units of `currency` per 1 USD.
create table if not exists public.fx_rates_daily (
  rate_date date not null,
  currency text not null,
  usd_rate numeric(14, 8) not null,
  fetched_at timestamptz not null default now(),
  primary key (rate_date, currency)
);

create index if not exists fx_rates_daily_currency_date_idx
on public.fx_rates_daily(currency, rate_date desc);

alter table public.fx_rates_daily enable row level security;

drop policy if exists "Anyone can read fx rates" on public.fx_rates_daily;
create policy "Anyone can read fx rates"
on public.fx_rates_daily for select
using (true);

drop policy if exists "Service role can manage fx rates" on public.fx_rates_daily;
create policy "Service role can manage fx rates"
on public.fx_rates_daily for all
using (auth.role() = 'service_role')
with check (auth.role() = 'service_role');
