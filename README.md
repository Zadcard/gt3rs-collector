# GT3 RS collector

This public repository contains only the cloud collector, connectivity probe, and workflow definitions. The website source, database, raw match payloads, backups, and credentials are stored separately.

- Planned checks: every 15 minutes, at minutes 7, 22, 37, and 52 UTC.
- GitHub may delay or skip scheduled runs. The site marks any match feed stale after 45 minutes without a successful check.
- League, playoff, and friendly results are validated and merged independently. A failed feed cannot erase archived matches.
- Manual retry: Actions → Collect club matches → Run workflow.
- Alerts: a run fails, and GitHub emails the owner, once when match feeds become stale (45 minutes without a successful check). Partial or empty checks pass and show in the site's health status. If another collection or backup holds the archive lease, the run skips cleanly.
- Main timer: a Cloudflare cron on the hub re-enables and dispatches this workflow whenever no check has started for 16 minutes. GitHub's schedule is a second trigger.
- The daily backup fails, and emails the owner, if the database passes the 400 MB free-tier warning threshold.
- Backups run daily at 04:11 UTC to a separate private repository using a repository-scoped deploy key. Never upload backup artifacts to this public repository.
- Secrets belong in GitHub Actions secrets, never source files or logs. There are no pull-request workflows with access to secrets.
- Public repository schedules may be disabled by GitHub after 60 days without repository activity. Check Actions settings and re-enable if needed; the site health indicator will flag stopped collection.

Collection success cannot recover matches that EA never exposed or that disappeared during an outage. Unknown statistics stay unlabeled in the private original payloads until supported by evidence.
