-- Real support ticketing: replaces the Support inbox admin page's static
-- mockup and the mobile app's plain mailto "Contact support" link with an
-- actual threaded conversation, tracked end to end.
--
-- Access model: service-role-only, matching data_requests
-- (20260816_create_data_requests_table.sql) and every other backend-owned
-- table. Both the mobile app (creating tickets/messages/attachments for
-- itself) and the admin console go through the FastAPI backend using their
-- own bearer tokens -- no direct PostgREST access from either client.
--
-- Run this once in the Supabase SQL editor.

create table if not exists public.support_tickets (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  category text not null check (category in ('bug', 'pricing', 'question', 'feedback')),
  subject text not null,
  status text not null default 'open' check (status in ('open', 'resolved')),
  referenced_item_id uuid,
  unread_by_admin boolean not null default true,
  unread_by_user boolean not null default false,
  first_response_at timestamptz,
  resolved_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.support_messages (
  id uuid primary key default gen_random_uuid(),
  ticket_id uuid not null references public.support_tickets(id) on delete cascade,
  sender_type text not null check (sender_type in ('user', 'admin')),
  sender_label text,
  body text not null,
  created_at timestamptz not null default now()
);

create table if not exists public.support_message_attachments (
  id uuid primary key default gen_random_uuid(),
  message_id uuid not null references public.support_messages(id) on delete cascade,
  file_path text not null,
  file_name text not null,
  content_type text,
  size_bytes integer,
  created_at timestamptz not null default now()
);

create index if not exists support_tickets_user_id_idx on public.support_tickets (user_id);
create index if not exists support_tickets_status_idx on public.support_tickets (status);
create index if not exists support_messages_ticket_id_idx on public.support_messages (ticket_id);
create index if not exists support_message_attachments_message_id_idx on public.support_message_attachments (message_id);

alter table public.support_tickets enable row level security;
alter table public.support_messages enable row level security;
alter table public.support_message_attachments enable row level security;
