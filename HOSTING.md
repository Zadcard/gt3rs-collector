# Activate live club collection

The website and database stay on Cloudflare. This service only reads EA's public feeds and writes through the authenticated hub ingestion API. It has no public HTTP port or local database.

## Recommended: Render Background Worker

Render background workers run continuously: https://render.com/docs/background-workers

1. In Render, create a Blueprint from `https://github.com/Zadcard/gt3rs-collector`, using `render.yaml` at the repository root.
2. Review the paid worker plan and current price before creating the service: https://render.com/pricing
3. Set `COLLECTOR_TOKEN` as a secret environment variable. It must match the hub's existing collector credential; never paste it into a chat or commit it. The Blueprint deliberately contains no value. GitHub Actions secrets cannot be downloaded from GitHub.
4. Deploy one instance. The Blueprint uses the smallest documented worker shape, `0.5c-512mb`, with 512 MB memory and 0.5 CPU. No disk or extra database is required.
5. Verify the dashboard says **Live checks every minute** for an active club or **Checks every 5 minutes** for an idle club. Confirm all three match feeds succeed and the latest successful-check timestamp advances across several cycles.
6. Verify EA access from the chosen host before relying on the latency target. A healthy process heartbeat alone does not prove EA is answering. If the host is blocked or rate-limited, saved data remains available and the dashboard reports delayed collection.

The prepared image has not been built locally because Docker's daemon is not running. The Python service and hub integration have automated tests. Render must build the image and pass the live verification above before one-minute collection is considered activated.

## Existing Docker server alternative

From this directory, provide the token in the host's protected secret environment, then run `docker compose up --build -d`. `compose.yml` runs the process without root privileges, restarts it on failure, gives it five minutes to finish on shutdown, and keeps the filesystem read-only except for a temporary heartbeat file.

## Scheduling and recovery

- Active: last match within three hours, or a refresh request within one hour. Due every minute. Idle: due every five minutes, including overnight.
- The process wakes within ten seconds for new work. Clubs are claimed in oldest-due order, two at a time, and processed independently. Targets depend on EA response time and queue capacity; more clubs may require more capacity and partitioned collectors.
- A server-enforced resident lease renews every 20 seconds and expires after 90 seconds. Only its owner can renew or release it. Starting a second instance does not double EA traffic.
- Cloudflare's existing five-minute cron dispatches the GitHub fallback when that lease expires. GitHub startup and queue time still apply. Search and daily backups remain independent.
- Archive writes and backups retain their shared lease protection. Individual club identities, raw payload retention, duplicate handling and fuller-stat preservation remain in place.
- The service survives individual club failures. Match-feed failures retry rapidly; the root collector's match backoff is capped at five minutes. Scoped club failures retry in one minute.
- Scoped status tracks successful checks per match feed, so a failed attempt cannot make stale data look fresh. Empty successful feeds are distinguished from unavailable feeds.
- Browser status polls every 15 seconds. New data reloads after five seconds without interaction, while form entry and sharing remain protected. Opening a saved club requests a check, subject to server freshness and rate limits.

No polling schedule can recover results that EA never publishes, or results that leave EA's recent window during an extended outage. Monitor real latency and successful feed checks rather than claiming zero missed games.
