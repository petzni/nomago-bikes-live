-- GitHub urnik (cron) je nezanesljiv, zato izračun statistike vsakih 15 min sproži Supabase
-- (pg_cron + pg_net -> GitHub API workflow_dispatch). Potrebuje žeton v nb.config:
--   insert into nb.config values ('gh_token', 'github_pat_...') on conflict (key) do update set value = excluded.value;
insert into nb.config values ('gh_repo', 'petzni/nomago-bikes-live') on conflict (key) do nothing;

create or replace function nb.trigger_build() returns void
language plpgsql as $$
declare tok text := (select value from nb.config where key = 'gh_token');
        repo text := (select value from nb.config where key = 'gh_repo');
begin
  if tok is null or tok = '' then return; end if;
  perform net.http_post(
    url := 'https://api.github.com/repos/' || repo || '/actions/workflows/pages.yml/dispatches',
    body := '{"ref":"main"}'::jsonb,
    headers := jsonb_build_object('Authorization', 'Bearer ' || tok, 'Accept', 'application/vnd.github+json',
                                  'User-Agent', 'nomago-bikes-live', 'X-GitHub-Api-Version', '2022-11-28',
                                  'Content-Type', 'application/json'),
    timeout_milliseconds := 15000);
end $$;

select cron.unschedule(jobid) from cron.job where jobname = 'nb-build';
select cron.schedule('nb-build', '3,18,33,48 * * * *', 'select nb.trigger_build()');
