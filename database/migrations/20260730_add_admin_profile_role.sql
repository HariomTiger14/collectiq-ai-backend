-- Admin portal role support. Admins remain normal Supabase Auth users;
-- the backend authorizes admin routes from their public.profiles role.
alter table if exists public.profiles
    add column if not exists role text not null default 'user',
    add column if not exists is_admin boolean not null default false;

create index if not exists profiles_role_idx on public.profiles (role);
create index if not exists profiles_is_admin_idx on public.profiles (is_admin);
