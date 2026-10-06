"""Cloud collector. Public source; credentials and raw data never enter logs/repo."""
import json
import os
import time
import re
from urllib.parse import urlencode
from curl_cffi import requests

HUB = os.environ.get('HUB_URL', 'https://gt3rs-hub.zadcard06.workers.dev').rstrip('/')
TOKEN = os.environ.get('COLLECTOR_TOKEN', '')
FEEDS = ('leagueMatch', 'playoffMatch', 'friendlyMatch')
CLUB = '205974'  # GT3 RS (club.json in the hub repo)
PLATFORM = 'common-gen5'


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
    endpoints = [(kind, f'clubs/matches?platform={PLATFORM}&clubIds={CLUB}&matchType={kind}&maxResultCount=10') for kind in FEEDS]
    endpoints += [('info', f'clubs/info?platform={PLATFORM}&clubIds={CLUB}'),
                  ('overallStats', f'clubs/overallStats?platform={PLATFORM}&clubIds={CLUB}'),
                  ('members', f'members/stats?platform={PLATFORM}&clubId={CLUB}')]
    try:
        for key, path in endpoints:
            wanted = plan.get(key, 'always')
            if wanted == 'skip' or (wanted == 'if-new' and not saved_new):
                states[key] = {'state': 'skipped'}
                print(key + ': skipped', flush=True)
                continue
            try:
                data = ea(path, deadline)
                if key in FEEDS:
                    if not isinstance(data, list) or len(data) > 10:
                        raise RuntimeError('Invalid match feed')
                    failures = 0
                    for match in data:
                        try:
                            if not isinstance(match, dict):
                                raise RuntimeError('Invalid match record')
                            match['_matchType'] = key
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
                    api('/admin/snapshot', {'runId': run_id, 'component': key, 'data': data})
                states[key] = {'state': 'ok', 'count': len(data) if key in FEEDS else None}
            except Exception as error:
                states[key] = {'state': 'error', 'reason': str(error)[:120]}
            print(key + ': ' + states[key]['state'], flush=True)
            time.sleep(1)
    finally:
        result = api('/admin/finish', {'runId': run_id, 'endpoints': states})
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


def run_registered(*, workers=1, return_count=False):
    """Collect each claimed club with its own identity; the hub owns scheduling and overlap control."""
    claimed = api('/admin/clubs/claim', {}, retries=0)
    lease = claimed.get('lease')
    def capture(club):
        platform, club_id = club['platform'], club['clubId']
        identity = {'platform': platform, 'clubId': club_id, 'runId': club['runId']}
        deadline = time.monotonic() + 170
        states = {}
        plan = club.get('plan') or {}
        saved_new = False
        endpoints = [(kind, f'clubs/matches?platform={platform}&clubIds={club_id}&matchType={kind}&maxResultCount=10') for kind in FEEDS]
        endpoints += [('info', f'clubs/info?platform={platform}&clubIds={club_id}'),
                      ('overallStats', f'clubs/overallStats?platform={platform}&clubIds={club_id}'),
                      ('members', f'members/stats?platform={platform}&clubId={club_id}')]
        try:
            for key, path in endpoints:
                wanted = plan.get(key, 'always')
                if wanted == 'skip' or (wanted == 'if-new' and not saved_new):
                    states[key] = {'state': 'skipped'}
                    continue
                try:
                    data = ea(path, deadline)
                    if key in FEEDS:
                        if not isinstance(data, list) or len(data) > 10:
                            raise RuntimeError('Invalid match feed')
                        failed = 0
                        for match in data:
                            try:
                                if not isinstance(match, dict):
                                    raise RuntimeError('Invalid match record')
                                match['_matchType'] = key
                                saved = api('/admin/clubs/match', {**identity, 'match': match})
                                if saved.get('state') in ('quarantined', 'held'):
                                    failed += 1
                                if saved.get('state') == 'new':
                                    saved_new = True
                            except RuntimeError:
                                failed += 1
                        if failed:
                            raise RuntimeError(f'{failed} match records failed persistence')
                    else:
                        api('/admin/clubs/snapshot', {**identity, 'component': key, 'data': data})
                    states[key] = {'state': 'ok'}
                except Exception as error:
                    states[key] = {'state': 'error', 'reason': str(error)[:120]}
                time.sleep(1)
        finally:
            result = api('/admin/clubs/finish', {**identity, 'endpoints': states})
        print('Registered club collection: ' + result['status'], flush=True)
    clubs = claimed.get('clubs', [])
    try:
        if workers > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(workers, 2)) as pool:
                futures = [pool.submit(capture, club) for club in clubs]
                for future in futures:
                    try:
                        future.result()
                    except Exception:
                        print('One club check failed; other clubs continue', flush=True)
        else:
            for club in clubs:
                try:
                    capture(club)
                except Exception:
                    print('One club check failed; other clubs continue', flush=True)
    finally:
        if lease:
            api('/admin/clubs/release', {'lease': lease})
    return len(clubs) if return_count else 0


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
            if not run_registered(workers=2, return_count=True):
                break
        raise SystemExit(legacy_result)
    except RuntimeError as error:
        print(str(error))
        raise SystemExit(1)
