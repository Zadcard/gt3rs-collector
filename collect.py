"""Cloud collector. Public source; credentials and raw data never enter logs/repo."""
import json
import os
import time
from curl_cffi import requests

HUB = os.environ.get('HUB_URL', 'https://gt3rs-hub.zadcard06.workers.dev').rstrip('/')
TOKEN = os.environ.get('COLLECTOR_TOKEN', '')
FEEDS = ('leagueMatch', 'playoffMatch', 'friendlyMatch')


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


def run():
    if not TOKEN:
        raise RuntimeError('Missing collector credential')
    run_id = api('/admin/start', {}, retries=0)['runId']
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
                            api('/admin/match', {'runId': run_id, 'match': match})
                        except RuntimeError:
                            failures += 1
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
    print('Collection: ' + result['status'])
    return 0 if result['status'] == 'success' else 1


if __name__ == '__main__':
    try:
        raise SystemExit(run())
    except RuntimeError as error:
        print(str(error))
        raise SystemExit(1)
