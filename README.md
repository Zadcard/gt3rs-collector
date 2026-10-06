# GT3 RS collector

This public repository contains only the cloud collector, connectivity probe, and workflow definitions. The website source, database, raw match payloads, backups, and credentials are stored separately.

- The hub decides when to check: every minute while the club is active (a match in the last 3 hours, or a Refresh request in the last hour) and every five minutes otherwise. These are resident-service targets. Without that service, the existing Cloudflare timer dispatches the GitHub fallback about every five minutes, plus runner startup and queue time. EA's recent window cannot guarantee no losses during an outage.
- Each run asks the hub what to fetch: all three match feeds; club record and squad stats every run while playing, otherwise only after a new match (or every 6 hours); club info daily. Failing match feeds retry after one minute, with backoff capped at five minutes. Non-match endpoints retain their longer backoff.
- The site marks match feeds stale after five minutes without a successful match-feed check while playing (15 minutes otherwise).
- League, playoff, and friendly results are validated and merged independently. A failed feed cannot erase archived matches.
- Match records the hub cannot read (for example after a game update changes EA's format) are kept raw for review instead of being dropped. The first such record fails one run, so GitHub emails the owner once per episode. A newer EA copy of a saved match with fewer players or stats is archived but never replaces the fuller copy.
- Manual retry: Actions → Collect club matches → Run workflow.
- Alerts: a run fails, and GitHub emails the owner, once when match feeds become stale. Partial or empty checks pass and show in the site's health status. If another collection or backup holds the archive lease, the run skips cleanly.
- Main timer: a Cloudflare cron on the hub re-enables and dispatches this workflow whenever a check is due. GitHub's own schedule (hourly, minute 37) is only a fallback: the hub turns those runs away while its timer is working.
- The daily backup fails, and emails the owner, if the database passes the 400 MB free-tier warning threshold.
- Each backup is test-restored into a throwaway in-memory database (using the hub's own table definitions) before it is saved, and the hub records it as complete only after the private push succeeds. The site's status page warns when no backup has completed for 30 hours.
- Backups run daily to a separate private repository using a repository-scoped deploy key. The hub's cron starts one whenever the last began over 24 hours ago and retries hourly if it never starts; the 04:11 UTC GitHub schedule is a second trigger. Never upload backup artifacts to this public repository.
- Secrets belong in GitHub Actions secrets, never source files or logs. There are no pull-request workflows with access to secrets.
- Public repository schedules may be disabled by GitHub after 60 days without repository activity. Check Actions settings and re-enable if needed; the site health indicator will flag stopped collection.

Collection success cannot recover matches that EA never exposed or that disappeared during an outage. Unknown statistics stay unlabeled in the private original payloads until supported by evidence.

## Resident service

`daemon.py`, `Dockerfile`, `compose.yml` and `render.yaml` prepare a continuously running service. See [HOSTING.md](HOSTING.md) for activation, secret handling, verification and fallback behavior. Preparing these files does not start a host; the website reports scheduled mode until the service is running and renewing its heartbeat.
