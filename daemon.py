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
# Several resident collectors can share the club queue, each on its own runner (resident.yml's slot input). Slot 1 also
# checks the founding club; the hub keeps one lease per slot so a slot never runs twice.
try:
    SLOT = max(1, min(int(os.environ.get('COLLECTOR_SLOT') or 1), 8))
except ValueError:
    SLOT = 1


def run_service(stop, *, max_seconds=0):
    if not collect.TOKEN:
        raise RuntimeError('Missing collector credential')
    owner = str(uuid.uuid4())
    # Acquire before doing any work: two deployed replicas must not both poll EA.
    collect.api('/admin/clubs/heartbeat', {'owner': owner, 'slot': SLOT}, retries=0)
    started = time.monotonic()
    next_root = 0

    def heartbeat():
        failures = 0
        while not stop.is_set():
            try:
                collect.api('/admin/clubs/heartbeat', {'owner': owner, 'slot': SLOT})
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
    # Tracked clubs: a pool of workers, each taking the next due club as soon as it is free.
    queue = threading.Thread(target=collect.run_pool, args=(stop, collect.WORKERS), name='collector-queue', daemon=True)
    queue.start()
    try:
        while not stop.is_set() and (not max_seconds or time.monotonic()-started < max_seconds):
            if SLOT == 1 and time.monotonic() >= next_root:
                try:
                    # Server enforces the club's due time even though this process stays alive.
                    result = collect.run()
                    if result:
                        print('Root club needs attention; collection continues', flush=True)
                except Exception:
                    print('Root club check failed; will retry', flush=True)
                next_root = time.monotonic() + 15
            stop.wait(1)
    finally:
        stop.set()
        # Checks already started finish (each stops within about three minutes) before the lease is given back.
        queue.join(timeout=200)
        thread.join(timeout=25)
        try:
            collect.api('/admin/clubs/heartbeat', {'owner': owner, 'slot': SLOT, 'stop': True})
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
