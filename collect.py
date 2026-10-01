"""Cloud collector. Public source; credentials and raw data never enter logs/repo."""
import json
import os
import time
from curl_cffi import requests

HUB = os.environ.get('HUB_URL', 'https://gt3rs-hub.zadcard06.workers.dev').rstrip('/')
TOKEN = os.environ.get('COLLECTOR_TOKEN', '')
FEEDS = ('leagueMatch', 'playoffMatch', 'friendlyMatch')
CLUB = '205974'


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
        stats = ea(f'clubs/overallStats?platform=common-gen5&clubIds={opponent}', deadline)
        api('/admin/opponent', {'runId': run_id, 'matchId': str(match.get('matchId')), 'clubId': opponent, 'data': stats})
    except Exception:
        print('Opponent strength unavailable for one match', flush=True)


def run():
    if not TOKEN:
        raise RuntimeError('Missing collector credential')
    try:
        run_id = api('/admin/start', {}, retries=0)['runId']
    except RuntimeError as error:
        if 'HTTP 409' in str(error):
            print('Another collection or backup is active; skipping this check')
            return 0
        raise
    deadline = time.monotonic() + 170
    states = {}
    endpoints = [(kind, f'clubs/matches?platform=common-gen5&clubIds=205974&matchType={kind}&maxResultCount=10') for kind in FEEDS]
    endpoints += [('info', 'clubs/info?platform=common-gen5&clubIds=205974'),
                  ('overallStats', 'clubs/overallStats?platform=common-gen5&clubIds=205974'),
                  ('members', 'members/stats?platform=common-gen5&clubId=205974')]
    try:
        for key, path in endpoints:
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
        print('ALERT: no successful match-feed check for over 45 minutes')
        return 1
    # Likewise once when EA starts sending match records the hub cannot read (they are kept for review).
    if result.get('reviewAlert'):
        print(f'ALERT: {result.get("unreadable")} match record(s) could not be read and were kept for review')
        return 1
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(run())
    except RuntimeError as error:
        print(str(error))
        raise SystemExit(1)
