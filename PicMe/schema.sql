-- PicMe database schema  (A2)  -  run this in Supabase: SQL Editor -> New query -> Run
-- Plain PostgreSQL, NO pgvector: the 512 face numbers are stored as a normal float array.
-- Every statement is idempotent: you can run this file as often as you like.

-- One row per event photo.
create table if not exists photos (
  id         text primary key,             -- Drive file id, or 'local:relative/path.jpg'
  name       text not null,                -- original filename, e.g. IMG_0456.jpg
  thumb_path text,                         -- path of the 600px thumbnail in Supabase Storage
  created_at timestamptz not null default now()
);

-- One row per detected face (a group photo has several rows, one per face).
create table if not exists faces (
  id        bigserial primary key,
  photo_id  text not null references photos(id) on delete cascade,
  embedding double precision[] not null    -- the 512 numbers (plain array, no pgvector)
);
create index if not exists faces_photo_id_idx on faces (photo_id);

-- One row per student session. ONLY the hash of the token is stored.
create table if not exists sessions (
  token_hash text primary key,             -- sha256 of the random token
  photo_ids  text[] not null,              -- which photos this student may download
  expires_at timestamptz not null
);
create index if not exists sessions_expires_at_idx on sessions (expires_at);

-- One row per admin export. Never stores a signed URL - only ids, link is built on demand.
create table if not exists exports (
  id         text primary key,
  photo_ids  text[] not null,
  part_no    int,
  created_at timestamptz not null default now(),
  expires_at timestamptz,
  token_hash text                         -- the session token currently handed out for it
);
-- The exports table existed BEFORE token_hash was added to this file, and
-- 'create table if not exists' can never add a column to an existing table.
-- This line can - and it is safe to run any number of times.
alter table exports add column if not exists token_hash text;

create index if not exists exports_part_no_idx on exports (part_no);

-- The table may already exist from an earlier run: make sure the extra
-- columns are there (this is a no-op when they already are).
alter table exports add column if not exists token_hash text;
alter table photos add column if not exists thumb_path text;
