"""Resident collector; no local database, credentials come only from the environment."""
import json
import os
from pathlib import Path
import signal
import threading
import time
import uuid
import collect

HEALTH_FILE = Path('/tmp/club-collector-health.json')


def run_service(stop, *, max_seconds=0):
    if not collect.TOKEN:
        raise RuntimeError('Missing collector credential')
    owner = str(uuid.uuid4())
    # Acquire before doing any work: two deployed replicas must not both poll EA.
    collect.api('/admin/clubs/heartbeat', {'owner': owner}, retries=0)
    started = time.monotonic()
    next_root = 0

    def heartbeat():
        failures = 0
        while not stop.is_set():
            try:
                collect.api('/admin/clubs/heartbeat', {'owner': owner})
                HEALTH_FILE.write_text(json.dumps({'heartbeat': time.time()}))
                failures = 0
            except Exception:
                failures += 1
                print('Collector heartbeat failed; retrying', flush=True)
                if failures >= 2:
                    stop.set()
                    return
            stop.wait(20)

    thread = threading.Thread(target=heartbeat, name='collector-heartbeat', daemon=True)
    thread.start()
    try:
        while not stop.is_set() and (not max_seconds or time.monotonic()-started < max_seconds):
            if time.monotonic() >= next_root:
                try:
                    # Server enforces the club's due time even though this process stays alive.
                    result = collect.run()
                    if result:
                        print('Root club needs attention; collection continues', flush=True)
                except Exception:
                    print('Root club check failed; will retry', flush=True)
                next_root = time.monotonic() + 15
            if stop.is_set():
                break
            try:
                count = collect.run_registered(workers=2, return_count=True)
            except Exception:
                print('Club queue check failed; will retry', flush=True)
                count = 0
            # Drain overdue clubs without sleeping between batches. An empty queue waits ten seconds.
            stop.wait(1 if count else 10)
    finally:
        stop.set()
        thread.join(timeout=25)
        try:
            collect.api('/admin/clubs/heartbeat', {'owner': owner, 'stop': True})
        except Exception:
            pass  # The server expires a dead process's lease automatically.
        HEALTH_FILE.unlink(missing_ok=True)


def main():
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    os.environ['GITHUB_EVENT_NAME'] = 'daemon'
    run_service(stop, max_seconds=float(os.environ.get('COLLECTOR_MAX_SECONDS', '0')))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Exceptions from HTTP libraries can contain request information; keep credentials out of logs.
        print('Resident collector stopped; check configuration and hub availability', flush=True)
        raise SystemExit(1)
