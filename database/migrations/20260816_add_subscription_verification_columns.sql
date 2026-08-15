-- Real Google Play / Apple subscription verification needs to look a user
-- back up from a Play purchase token (Real-Time Developer Notifications
-- webhook) or an Apple original transaction id (App Store Server
-- Notifications V2 webhook) -- neither of which is user_id, the column
-- public.user_subscriptions is currently keyed and looked up by. Without
-- these, a renewal/cancellation notification has no way to find the row
-- it needs to update.
--
-- Run this once in the Supabase SQL editor.

alter table public.user_subscriptions
  add column if not exists purchase_token text,
  add column if not exists original_transaction_id text,
  add column if not exists product_id text;

create index if not exists user_subscriptions_purchase_token_idx
  on public.user_subscriptions (purchase_token)
  where purchase_token is not null;

create index if not exists user_subscriptions_original_transaction_id_idx
  on public.user_subscriptions (original_transaction_id)
  where original_transaction_id is not null;
