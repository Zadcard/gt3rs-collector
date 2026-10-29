"""Cloud collector. Public source; credentials and raw data never enter logs/repo."""
import base64
import json
import os
import time
import re
import threading
from urllib.parse import urlencode
from curl_cffi import requests

HUB = os.environ.get('HUB_URL', 'https://clubskeep.zadcard06.workers.dev').rstrip('/')
TOKEN = os.environ.get('COLLECTOR_TOKEN', '')
FEEDS = ('leagueMatch', 'playoffMatch', 'friendlyMatch')
CLUB = '205974'  # GT3 RS (club.json in the hub repo)
PLATFORM = 'common-gen5'
# Clubs checked at the same time. EA's load is set by EA_RATE below, not by this number: more workers only keep that
# pace filled while other clubs wait on the hub.
try:
    WORKERS = max(1, min(int(os.environ.get('COLLECTOR_WORKERS') or 8), 16))
except ValueError:
    WORKERS = 8
# EA requests per second from this collector, shared by all its workers. Raise slowly: EA may start refusing
# requests if too many arrive at once. Each extra resident collector runs on another runner with its own pace.
try:
    EA_RATE = max(0.5, min(float(os.environ.get('COLLECTOR_EA_RPS') or 4), 10))
except ValueError:
    EA_RATE = 4.0


class Pace:
    """Spaces requests evenly across threads: each caller gets the next free slot and waits for it."""
    def __init__(self, rate):
        self.gap, self.next, self.lock = 1 / rate, 0.0, threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next)
            self.next = at + self.gap
        if at > now:
            time.sleep(at - now)


EA_PACE = Pace(EA_RATE)


def api(path, payload, token=None, retries=1):
    for attempt in range(retries + 1):
        try:
            response = requests.post(HUB + path, json=payload, impersonate='chrome',
                headers={'Authorization': 'Bearer ' + (token if token is not None else TOKEN)},
                timeout=20, allow_redirects=False)
            if response.status_code == 200:
                return response.json()
            if response.status_code < 500 or attempt == retries:
                raise RuntimeError(f'Hub {path}: HTTP {response.status_code}')
        except RuntimeError:
            raise
        except Exception:
            if attempt == retries:
                raise RuntimeError(f'Hub {path}: connection failed') from None
        time.sleep(2)


def ea(path, deadline):
    for attempt in range(2):
        if time.monotonic() > deadline:
            raise RuntimeError('Run time budget reached')
        EA_PACE.wait()
        try:
            response = requests.get('https://proclubs.ea.com/api/fc/' + path,
                impersonate='chrome', timeout=12,
                headers={'Accept': 'application/json', 'Referer': 'https://proclubs.ea.com/'})
            if response.status_code != 200:
                raise RuntimeError(f'EA HTTP {response.status_code}')
            if len(response.content) > 2_000_000:
                raise RuntimeError('EA response too large')
            return response.json()
        except Exception as error:
            if attempt:
                raise RuntimeError(str(error) if isinstance(error, RuntimeError) else type(error).__name__) from None
            time.sleep(2)


CREST_URL = 'https://eafc24.content.easports.com/fifa/fltOnlineAssets/24B23FDE-7835-41C2-87A2-F453DFDB2E82/2024/fcweb/crests/256x256/l{}.png'


def crest_ids(data):
    """EA crest asset IDs in a match (both clubs) or a clubs/info response."""
    clubs = data.get('clubs', {}) if isinstance(data, dict) and 'clubs' in data else data
    ids = set()
    if isinstance(clubs, dict):
        for club in clubs.values():
            if not isinstance(club, dict):
                continue
            kit = (club.get('details') or club).get('customKit') if isinstance(club.get('details') or club, dict) else None
            value = str(kit.get('crestAssetId', '')).strip() if isinstance(kit, dict) else ''
            if re.fullmatch(r'[1-9][0-9]{0,11}', value):
                ids.add(value)
    return ids


def sync_crests(ids, deadline):
    """Upload crests the hub doesn't have yet. Best effort: never affects collection."""
    if not ids:
        return
    try:
        missing = api('/admin/crests/missing', {'ids': sorted(ids)[:100]}).get('missing', [])
        for crest in missing[:20]:
            if time.monotonic() > deadline:
                break
            try:
                response = requests.get(CREST_URL.format(crest), impersonate='chrome', timeout=10)
                if response.status_code != 200 or not response.content.startswith(b'\x89PNG') or len(response.content) > 300_000:
                    continue
                api('/admin/crests/upload', {'id': crest, 'png': base64.b64encode(response.content).decode()})
            except Exception:
                continue
    except Exception:
        print('Crests unavailable this run', flush=True)


def division_row(platform, club_id, name, deadline):
    """The club's row from EA's leaderboard search (it carries currentDivision); found by name, matched by ID.
    The current-season board comes first: after the playoffs the all-time board's currentDivision stops updating
    (GT3 RS read 7 there and 5 on the season board while the game showed Division 4)."""
    if not isinstance(name, str) or not name.strip():
        raise RuntimeError('Club name unknown')
    for endpoint in ('currentSeasonLeaderboard/search', 'allTimeLeaderboard/search'):
        data = ea(endpoint + '?' + urlencode({'platform': platform, 'clubName': name.strip(), 'maxResultCount': 100}), deadline)
        items = list(data.values()) if isinstance(data, dict) else data if isinstance(data, list) else []
        for item in items:
            if isinstance(item, dict) and str(item.get('clubId') or (item.get('clubInfo') or {}).get('clubId') or '') == club_id:
                return {**item, 'clubId': club_id}
    raise RuntimeError('Club not found in EA leaderboard search')


def club_name(data, club_id, fallback):
    """Name from a clubs/info response, else the name the hub sent."""
    info = data.get(club_id) if isinstance(data, dict) else None
    name = info.get('name') if isinstance(info, dict) else None
    return name if isinstance(name, str) and name.strip() else fallback


def save_opponent(run_id, match, deadline):
    """Record the opponent's EA record right after a new match. Best effort: never affects saving the match."""
    try:
        opponent = next(c for c in match.get('clubs', {}) if c != CLUB)
        stats = ea(f'clubs/overallStats?platform={PLATFORM}&clubIds={opponent}', deadline)
        api('/admin/opponent', {'runId': run_id, 'matchId': str(match.get('matchId')), 'clubId': opponent, 'data': stats})
    except Exception:
        print('Opponent strength unavailable for one match', flush=True)


def run():
    if not TOKEN:
        raise RuntimeError('Missing collector credential')
    try:
        start = api('/admin/start', {'trigger': os.environ.get('GITHUB_EVENT_NAME', '')}, retries=0)
    except RuntimeError as error:
        if 'HTTP 409' in str(error):
            print('Another collection or backup is active; skipping this check')
            return 0
        raise
    # The hub schedules checks; GitHub's own schedule only runs when the hub's timer has stopped.
    if start.get('skip'):
        print('Not due yet (the hub schedules checks); skipping this one')
        return 0
    run_id = start['runId']
    # What to ask EA for this run: 'always', 'if-new' (only after saving a new match) or 'skip'. An older hub
    # sends no plan, which means everything.
    plan = start.get('plan') or {}
    if start.get('mode'):
        print('Mode: ' + start['mode'], flush=True)
    saved_new = False
    deadline = time.monotonic() + 170
    states = {}
    crests = set()
    endpoints = [(kind, f'clubs/matches?platform={PLATFORM}&clubIds={CLUB}&matchType={kind}&maxResultCount=10') for kind in FEEDS]
    endpoints += [('info', f'clubs/info?platform={PLATFORM}&clubIds={CLUB}'),
                  ('overallStats', f'clubs/overallStats?platform={PLATFORM}&clubIds={CLUB}'),
                  ('members', f'members/stats?platform={PLATFORM}&clubId={CLUB}'),
                  ('division', None)]
    # Older hubs neither plan nor accept the division; leave it out entirely for them.
    endpoints = [(key, path) for key, path in endpoints if key != 'division' or 'division' in plan]
    name = start.get('clubName')
    try:
        for key, path in endpoints:
            wanted = plan.get(key, 'always')
            if wanted == 'skip' or (wanted == 'if-new' and not saved_new):
                states[key] = {'state': 'skipped'}
                print(key + ': skipped', flush=True)
                continue
            try:
                data = division_row(PLATFORM, CLUB, name, deadline) if key == 'division' else ea(path, deadline)
                if key in FEEDS:
                    if not isinstance(data, list) or len(data) > 10:
                        raise RuntimeError('Invalid match feed')
                    failures = 0
                    for match in data:
                        try:
                            if not isinstance(match, dict):
                                raise RuntimeError('Invalid match record')
                            match['_matchType'] = key
                            crests |= crest_ids(match)
                            saved = api('/admin/match', {'runId': run_id, 'match': match})
                        except RuntimeError:
                            failures += 1
                            continue
                        if isinstance(saved, dict) and saved.get('state') == 'new':
                            saved_new = True
                            save_opponent(run_id, match, deadline)
                    if failures:
                        raise RuntimeError(f'{failures} match records failed validation or persistence')
                else:
                    if key == 'info':
                        crests |= crest_ids(data)
                        name = club_name(data, CLUB, name)
                    api('/admin/snapshot', {'runId': run_id, 'component': key, 'data': data})
                states[key] = {'state': 'ok', 'count': len(data) if key in FEEDS else None}
            except Exception as error:
                states[key] = {'state': 'error', 'reason': str(error)[:120]}
            print(key + ': ' + states[key]['state'], flush=True)
    finally:
        result = api('/admin/finish', {'runId': run_id, 'endpoints': states})
    sync_crests(crests, time.monotonic() + 30)
    print('Collection: ' + result['status'] + (' (stale)' if result.get('stale') else ''))
    if result.get('recovered'):
        print('Match feeds recovered after a stale period')
    # Fail the run (GitHub emails the owner) once per stale episode, not on every partial check.
    if result.get('alert'):
        print('ALERT: match-feed checks are overdue')
        return 1
    # Likewise once when EA starts sending match records the hub cannot read (they are kept for review).
    if result.get('reviewAlert'):
        print(f'ALERT: {result.get("unreadable")} match record(s) could not be read and were kept for review')
        return 1
    return 0


def save_matches(identity, records):
    """Save one feed's matches in a single request; per-match requests remain for hubs without the batch route."""
    if not records:
        return []
    try:
        states = api('/admin/clubs/matches', {**identity, 'matches': records}).get('states')
    except RuntimeError as error:
        if 'HTTP 404' not in str(error):
            raise
        states = []
        for match in records:
            try:
                states.append(api('/admin/clubs/match', {**identity, 'match': match}).get('state'))
            except RuntimeError:
                states.append('failed')
    if not isinstance(states, list) or len(states) != len(records):
        raise RuntimeError('Hub did not report every match')
    return states


def capture_club(club):
    """Check one claimed club with its own identity and report every endpoint's result to the hub."""
    platform, club_id = club['platform'], club['clubId']
    identity = {'platform': platform, 'clubId': club_id, 'runId': club['runId']}
    deadline = time.monotonic() + 170
    states = {}
    plan = club.get('plan') or {}
    saved_new = False
    crests = set()
    endpoints = [(kind, f'clubs/matches?platform={platform}&clubIds={club_id}&matchType={kind}&maxResultCount=10') for kind in FEEDS]
    endpoints += [('info', f'clubs/info?platform={platform}&clubIds={club_id}'),
                  ('overallStats', f'clubs/overallStats?platform={platform}&clubIds={club_id}'),
                  ('members', f'members/stats?platform={platform}&clubId={club_id}'),
                  ('division', None)]
    endpoints = [(key, path) for key, path in endpoints if key != 'division' or 'division' in plan]
    name = club.get('name')
    try:
        for key, path in endpoints:
            wanted = plan.get(key, 'always')
            if wanted == 'skip' or (wanted == 'if-new' and not saved_new):
                states[key] = {'state': 'skipped'}
                continue
            try:
                data = division_row(platform, club_id, name, deadline) if key == 'division' else ea(path, deadline)
                if key in FEEDS:
                    if not isinstance(data, list) or len(data) > 10:
                        raise RuntimeError('Invalid match feed')
                    records = [match for match in data if isinstance(match, dict)]
                    failed = len(data) - len(records)
                    for match in records:
                        match['_matchType'] = key
                        crests |= crest_ids(match)
                    for state in save_matches(identity, records):
                        if state in ('quarantined', 'held', 'failed'):
                            failed += 1
                        if state == 'new':
                            saved_new = True
                    if failed:
                        raise RuntimeError(f'{failed} match records failed persistence')
                else:
                    if key == 'info':
                        crests |= crest_ids(data)
                        name = club_name(data, club_id, name)
                    api('/admin/clubs/snapshot', {**identity, 'component': key, 'data': data})
                states[key] = {'state': 'ok'}
            except Exception as error:
                states[key] = {'state': 'error', 'reason': str(error)[:120]}
    finally:
        result = api('/admin/clubs/finish', {**identity, 'endpoints': states})
    sync_crests(crests, time.monotonic() + 30)
    print('Registered club collection: ' + result['status'], flush=True)


def run_registered(*, workers=1, return_count=False):
    """Collect one claimed batch of clubs; the hub owns scheduling and overlap control."""
    claimed = api('/admin/clubs/claim', {'limit': workers}, retries=0)
    lease = claimed.get('lease')
    clubs = claimed.get('clubs', [])
    try:
        if workers > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(workers, 16)) as pool:
                futures = [pool.submit(capture_club, club) for club in clubs]
                for future in futures:
                    try:
                        future.result()
                    except Exception:
                        print('One club check failed; other clubs continue', flush=True)
        else:
            for club in clubs:
                try:
                    capture_club(club)
                except Exception:
                    print('One club check failed; other clubs continue', flush=True)
    finally:
        if lease:
            api('/admin/clubs/release', {'lease': lease})
    return len(clubs) if return_count else 0


def run_pool(stop, workers=None, idle_seconds=10):
    """Keep `workers` club checks running until `stop` is set. A worker that finishes takes the next due club at
    once, so no club waits for the slowest check of a batch; EA_PACE keeps EA's load the same at any worker count."""
    from concurrent.futures import ThreadPoolExecutor
    workers = workers or WORKERS
    free = threading.Semaphore(workers)
    checked = 0

    def check(club):
        try:
            capture_club(club)
        except Exception:
            print('One club check failed; other clubs continue', flush=True)
        finally:
            free.release()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        while not stop.is_set():
            if not free.acquire(timeout=1):
                continue
            wanted = 1
            while wanted < min(workers, 8) and free.acquire(blocking=False):
                wanted += 1
            try:
                clubs = api('/admin/clubs/claim', {'limit': wanted}, retries=0).get('clubs', [])
            except Exception:
                print('Club queue check failed; will retry', flush=True)
                clubs = []
            for _ in range(wanted - len(clubs)):
                free.release()
            for club in clubs:
                pool.submit(check, club)
            checked += len(clubs)
            # Fewer clubs than asked for means the queue is empty for now.
            if len(clubs) < wanted:
                stop.wait(idle_seconds)
    return checked


def search_clubs(query, platform, deadline):
    """Search public club names; no EA account login or numeric ID needed."""
    results = {}
    succeeded = False
    for endpoint in ('allTimeLeaderboard/search', 'currentSeasonLeaderboard/search'):
        try:
            data = ea(endpoint + '?' + urlencode({'platform': platform, 'clubName': query, 'maxResultCount': 100}), deadline)
            if not isinstance(data, (list, dict)):
                raise RuntimeError('Invalid search response')
            items = list(data.values()) if isinstance(data, dict) else data
            if any(not isinstance(item, dict) for item in items):
                raise RuntimeError('Invalid search response')
            for item in items:
                if not isinstance(item, dict):
                    continue
                info = item.get('clubInfo') or item
                club_id = str(info.get('clubId') or item.get('clubId') or '')
                name = info.get('name') or item.get('clubName') or item.get('name')
                if re.fullmatch(r'[0-9]{1,20}', club_id) and isinstance(name, str) and 0 < len(name.strip()) <= 120 and not re.search(r'[\x00-\x1f]', name):
                    results[club_id] = {'clubId': club_id, 'name': name.strip()}
            succeeded = True
        except Exception:
            continue
    if not succeeded:
        raise RuntimeError('EA club search unavailable')
    return list(results.values())[:30]


def run_searches():
    jobs = api('/admin/clubs/search/claim', {})['jobs']
    deadline = time.monotonic() + 120
    for job in jobs:
        payload = {'id': job['id'], 'runId': job['runId'], 'results': []}
        try:
            payload['results'] = search_clubs(job['query'], job['platform'], deadline)
        except Exception:
            payload['failed'] = True
        api('/admin/clubs/search/result', payload)
    print(f'Completed {len(jobs)} club searches', flush=True)


if __name__ == '__main__':
    try:
        run_searches()
        legacy_result = run()
        drain_until = time.monotonic() + 120
        while time.monotonic() < drain_until:
            if not run_registered(workers=WORKERS, return_count=True):
                break
        raise SystemExit(legacy_result)
    except RuntimeError as error:
        print(str(error))
        raise SystemExit(1)
