"""Run on the actual hosted runner; log shape and status, never raw players."""
import json
import time
from curl_cffi import requests

paths = [
    'clubs/info?platform=common-gen5&clubIds=205974',
    'clubs/overallStats?platform=common-gen5&clubIds=205974',
    'members/stats?platform=common-gen5&clubId=205974',
] + [f'clubs/matches?platform=common-gen5&clubIds=205974&matchType={kind}&maxResultCount=10'
     for kind in ('leagueMatch', 'playoffMatch', 'friendlyMatch')]
results = []
for path in paths:
    try:
        r = requests.get('https://proclubs.ea.com/api/fc/' + path, impersonate='chrome',
                         headers={'Accept': 'application/json', 'Referer': 'https://proclubs.ea.com/'}, timeout=15)
        data = r.json() if r.status_code == 200 else None
        results.append({'endpoint': path.split('?')[0], 'status': r.status_code,
                        'json': isinstance(data, (dict, list)),
                        'count': len(data) if isinstance(data, (dict, list)) else None})
    except Exception as e:
        results.append({'endpoint': path.split('?')[0], 'error': type(e).__name__})
    time.sleep(1)
print(json.dumps(results, indent=2))
raise SystemExit(0 if all(r.get('status') == 200 and r.get('json') for r in results) else 1)
