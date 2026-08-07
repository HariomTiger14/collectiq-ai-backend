alter table public.pricecharting_set_registry
    add column if not exists console_uid text;

create index if not exists pricecharting_set_registry_console_uid_idx
    on public.pricecharting_set_registry (source_site, console_uid)
    where console_uid is not null;
