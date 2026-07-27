create index if not exists pricecharting_catalog_source_imported_idx
    on public.pricecharting_catalog (source_file, imported_at desc);

create index if not exists pricecharting_catalog_history_source_current_idx
    on public.pricecharting_catalog_history (source_file, is_current);

create or replace function public.pricecharting_catalog_health_summary()
returns table (
    source_file text,
    current_rows bigint,
    history_rows bigint,
    closed_history_rows bigint,
    last_loaded_at timestamptz
)
language sql
stable
security definer
set search_path = public
as $$
    with expected_sources(source_file) as (
        values
            ('magic.csv'),
            ('one_piece.csv'),
            ('pokemon.csv'),
            ('video_games.csv'),
            ('yugioh.csv')
    ),
    catalog_summary as (
        select
            pc.source_file,
            count(*)::bigint as current_rows,
            max(coalesce(pc.source_downloaded_at, pc.imported_at, pc.updated_at)) as last_loaded_at
        from public.pricecharting_catalog pc
        where pc.source_file is not null
        group by pc.source_file
    ),
    history_summary as (
        select
            pch.source_file,
            count(*)::bigint as history_rows,
            count(*) filter (where pch.is_current = false)::bigint as closed_history_rows
        from public.pricecharting_catalog_history pch
        where pch.source_file is not null
        group by pch.source_file
    )
    select
        expected_sources.source_file,
        coalesce(catalog_summary.current_rows, 0)::bigint as current_rows,
        coalesce(history_summary.history_rows, 0)::bigint as history_rows,
        coalesce(history_summary.closed_history_rows, 0)::bigint as closed_history_rows,
        catalog_summary.last_loaded_at
    from expected_sources
    left join catalog_summary
        on catalog_summary.source_file = expected_sources.source_file
    left join history_summary
        on history_summary.source_file = expected_sources.source_file
    order by expected_sources.source_file;
$$;

grant execute on function public.pricecharting_catalog_health_summary()
    to service_role;
