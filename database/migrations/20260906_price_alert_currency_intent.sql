-- Price alerts: keep the collector's intent and the comparison value apart.
--
-- A threshold means two different things. "Alert me at AUD 50" is what the
-- collector chose and what the app must show back to them. The evaluator
-- compares against an item price stored in the provider's own currency (USD),
-- so it needs that same threshold in USD. One column cannot be both: with
-- item prices now stored natively, an AUD 50 target was being compared
-- against a USD figure.
--
-- target_amount keeps its existing meaning for the evaluator -- the value to
-- compare -- and is written in USD for new alerts. The columns below record
-- what the collector actually chose, plus the rate behind the conversion so
-- it can be checked or redone rather than being an unexplained number.
--
-- All nullable and additive. Existing rows have no display_currency, which
-- means "created before this was tracked" and is read as AUD by the app --
-- correct, because everything was stored in AUD then.
--
-- OPTIONAL for the app to work. The app persists the same intent inside
-- raw_json, and the evaluator reads either, so alerts save and fire whether
-- or not this has been applied. These columns exist so the admin portal and
-- any SQL reporting can see a threshold's real currency without unpacking
-- JSON. Apply it, then populate from raw_json if the history matters:
--
--   update public.price_alerts
--      set display_amount   = (raw_json -> 'rule' ->> 'amount')::numeric,
--          display_currency =  raw_json -> 'rule' ->> 'displayCurrency'
--    where display_currency is null
--      and raw_json -> 'rule' ->> 'displayCurrency' is not null;

alter table public.price_alerts
  add column if not exists display_amount numeric,
  add column if not exists display_currency text,
  add column if not exists normalized_amount_usd numeric,
  add column if not exists exchange_rate_used numeric,
  add column if not exists exchange_rate_date timestamptz;

comment on column public.price_alerts.display_amount is
  'The threshold as the collector entered it, in display_currency. Intent, not a comparison value.';
comment on column public.price_alerts.display_currency is
  'Currency display_amount was entered in. Null means the alert predates this and was AUD.';
comment on column public.price_alerts.normalized_amount_usd is
  'display_amount converted to USD, which is what item prices are stored in. Mirrored into target_amount for the evaluator.';
comment on column public.price_alerts.exchange_rate_used is
  'Rate applied to produce normalized_amount_usd, so the conversion is checkable.';
comment on column public.price_alerts.exchange_rate_date is
  'When exchange_rate_used was read.';
