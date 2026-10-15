-- Interni podatki (partnerji ipd.), ki jih piše builder.py. Bere jih le prijavljen uporabnik (Supabase Auth).
create table if not exists nb.internal_files (path text primary key, body jsonb not null, updated_at timestamptz default now());
alter table nb.internal_files enable row level security;

create or replace function public.nb_internal(p text default 'bundle') returns jsonb
language plpgsql stable security definer set search_path = nb, public as $$
begin
  if auth.uid() is null then
    raise exception 'prijava je obvezna' using errcode = '42501';
  end if;
  return (select body from nb.internal_files where path = p);
end $$;

revoke all on function public.nb_internal(text) from public, anon;
grant execute on function public.nb_internal(text) to authenticated;
