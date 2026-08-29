-- Pokemon card images from TCGdex (api.tcgdex.net), the reviewer-approved
-- production image source for Pokemon: free/no-key API, MIT-licensed
-- database, documented display model (low.webp 245x337 thumbnails,
-- high.webp 600x825 detail images), and measured 92.7% image coverage of
-- its 23.5K English cards (flat across eras -- see
-- packlox-card-image-source-licensing memory, 2026-08-29 audits).
-- Replaces the 5-set tcgplayer_pokemon_catalog path as primary; that
-- table stays as fallback until TCGdex is validated, then retires.
--
-- One row per card per language ('en'/'ja'). image_url is the TCGdex
-- asset BASE url (no quality suffix) exactly as the API returns it, null
-- when TCGdex has no image yet -- readers append /high.webp etc.
-- Japanese rows are matched via a hand-verified console_name -> set_name
-- mapping (app/services/pricing/tcgdex_pokemon_sets.py), never fuzzily.

create table if not exists public.tcgdex_pokemon_catalog (
    language text not null,
    set_id text not null,
    set_name text not null,
    -- normalized set name for deterministic lookups: lowercased,
    -- punctuation stripped (computed by the import script, matching
    -- tcgdex_pokemon_sets.normalize_set_key)
    set_key text not null,
    local_id text not null,
    -- leading zeros stripped + lowercased, for number equality with
    -- PriceCharting's "#4" style numbers
    local_id_norm text not null,
    card_name text,
    image_url text,
    imported_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (language, set_id, local_id)
);

-- The enrichment lookup path: (language, set_key, local_id_norm) for
-- English; (language, set_name, local_id_norm) for Japanese (set_name is
-- the exact Japanese name the hand map targets).
create index if not exists tcgdex_pokemon_catalog_en_lookup
    on public.tcgdex_pokemon_catalog (language, set_key, local_id_norm);
create index if not exists tcgdex_pokemon_catalog_ja_lookup
    on public.tcgdex_pokemon_catalog (language, set_name, local_id_norm);

-- Service-role-only, same posture as every other catalog table.
alter table public.tcgdex_pokemon_catalog enable row level security;
