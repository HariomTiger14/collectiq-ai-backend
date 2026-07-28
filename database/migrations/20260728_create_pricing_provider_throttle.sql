create table if not exists public.pricing_provider_throttle (
    provider_key text primary key,
    last_request_at timestamptz not null default 'epoch'::timestamptz,
    updated_at timestamptz not null default now()
);

alter table public.pricing_provider_throttle enable row level security;

create or replace function public.acquire_pricing_provider_throttle(
    provider_key_arg text,
    min_interval_ms_arg integer
)
returns table(acquired boolean, retry_after_ms integer)
language plpgsql
security definer
set search_path = public
as $$
declare
    now_value timestamptz;
    last_value timestamptz;
    wait_ms integer;
begin
    if provider_key_arg is null or btrim(provider_key_arg) = '' then
        return query select false, greatest(min_interval_ms_arg, 1000);
        return;
    end if;

    if min_interval_ms_arg is null or min_interval_ms_arg <= 0 then
        return query select true, 0;
        return;
    end if;

    perform pg_advisory_xact_lock(hashtext('pricing_provider_throttle:' || provider_key_arg));

    insert into public.pricing_provider_throttle (provider_key, last_request_at)
    values (provider_key_arg, 'epoch'::timestamptz)
    on conflict (provider_key) do nothing;

    select last_request_at
    into last_value
    from public.pricing_provider_throttle
    where provider_key = provider_key_arg
    for update;

    now_value := clock_timestamp();
    wait_ms := min_interval_ms_arg
        - floor(extract(epoch from (now_value - last_value)) * 1000)::integer;

    if wait_ms > 0 then
        return query select false, wait_ms;
        return;
    end if;

    update public.pricing_provider_throttle
    set last_request_at = now_value,
        updated_at = now()
    where provider_key = provider_key_arg;

    return query select true, 0;
end;
$$;
