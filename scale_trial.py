"""Bounded real-source capacity trial. Uses only its dedicated archive and credentials."""
import argparse
import concurrent.futures
import json
import os
import re
import statistics
import time
from urllib.parse import urlencode
import collect
from curl_cffi import requests

TRIAL = 'https://clubskeep-scale-trial-20261008.zadcard06.workers.dev'
if collect.HUB != TRIAL or not collect.TOKEN:
    raise RuntimeError('Dedicated trial destination and credential required')
HEADERS = {'Authorization': 'Bearer ' + collect.TOKEN}


def status():
    r = requests.get(TRIAL + '/admin/scale/status', headers=HEADERS,
                     impersonate='chrome', timeout=30, allow_redirects=False)
    if r.status_code != 200:
        raise RuntimeError(f'Trial status HTTP {r.status_code}')
    return r.json()


def percentile(xs, p):
    return sorted(xs)[max(0, min(len(xs)-1, int(len(xs)*p+.999999)-1))]


def benchmark(phase):
    summary = {}
    for key, path in [('rankings', '/api/hub/rankings'),
                      ('directory', '/api/hub/directory?q=FC')]:
        samples, sizes, failures = [], [], 0
        for _ in range(12):
            start = time.monotonic()
            try:
                r = requests.get(TRIAL + path, impersonate='chrome', timeout=30,
                                 allow_redirects=False)
                samples.append((time.monotonic()-start)*1000)
                sizes.append(len(r.content))
                if r.status_code != 200:
                    failures += 1
            except Exception:
                failures += 1
        summary[key] = {'samples': len(samples), 'failures': failures,
                        'medianMs': round(statistics.median(samples), 1) if samples else None,
                        'p95Ms': round(percentile(samples, .95), 1) if samples else None,
                        'maxResponseBytes': max(sizes, default=0)}
    snapshot = status()
    collect.api('/admin/scale/metrics', {'phase': phase, 'metrics': {
        'http': summary, 'counts': snapshot['counts'], 'health': snapshot['health'],
        'runnerRegion': 'GitHub-hosted; network-inclusive HTTP timings, not browser FPS'}})
    print(json.dumps({'phase': phase, 'counts': snapshot['counts'], 'http': summary}), flush=True)


def leaderboard_candidates():
    # Only current-season public leaderboard searches. Bounded to 80 searches.
    seen = set()
    prefixes = ['fc', 'united', 'city', 'club', 'team', 'a', 'e', 'i', 'o', 'u',
                's', 'n', 'r', 't', 'l', 'm', 'b', 'c', 'd', 'f', 'g', 'h',
                'j', 'k', 'p', 'v', 'w', 'y', 'z', 'real', 'the', 'red',
                'black', 'white', 'blue', 'dark', 'elite', 'star', 'inter', 'sport']
    for platform in ['common-gen5', 'common-gen4']:
        for prefix in prefixes:
            try:
                data = collect.ea('currentSeasonLeaderboard/search?' + urlencode({
                    'platform': platform, 'clubName': prefix, 'maxResultCount': 100}),
                    time.monotonic()+30)
            except Exception:
                print('One leaderboard search unavailable', flush=True)
                continue
            items = list(data.values()) if isinstance(data, dict) else data
            if not isinstance(items, list):
                continue
            fresh = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                info = item.get('clubInfo') or item
                club_id = str(info.get('clubId') or item.get('clubId') or '')
                name = info.get('name') or item.get('clubName') or item.get('name')
                key = (platform, club_id)
                if (key in seen or club_id == '205974' or
                    not re.fullmatch(r'\d{1,20}', club_id) or not isinstance(name, str) or
                    not 0 < len(name.strip()) <= 120 or re.search(r'[\x00-\x1f]', name)):
                    continue
                seen.add(key)
                fresh.append({'platform': platform, 'clubId': club_id, 'name': name.strip()})
            yield fresh
            time.sleep(1)


def recently_active(club):
    try:
        data = collect.ea('clubs/matches?' + urlencode({'platform': club['platform'],
            'clubIds': club['clubId'], 'matchType': 'leagueMatch', 'maxResultCount': 10}),
            time.monotonic()+30)
        if not isinstance(data, list):
            return None
        now = int(time.time())
        dates = [int(m['timestamp']) for m in data if isinstance(m, dict)
                 and str(m.get('timestamp', '')).isdigit()
                 and club['clubId'] in (m.get('clubs') or {})]
        latest = max(dates, default=0)
        if now-7*86400 <= latest <= now+300:
            return {**club, 'activityVerifiedAt': now, 'latestLeagueMatch': latest}
    except Exception:
        pass
    return None


def discover():
    s = status()
    if s['trial']['startedAt'] or s['counts']['clubs']:
        raise RuntimeError('Cohort already initialized; refusing to restart its clock')
    password = os.environ.get('SCALE_TRIAL_PASSWORD')
    if not password:
        raise RuntimeError('Trial account password required')
    collect.api('/admin/scale/account', {'password': password})
    benchmark('baseline-0')
    cohort, checked = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        for candidates in leaderboard_candidates():
            for club in pool.map(recently_active, candidates):
                checked += 1
                if club:
                    cohort.append(club)
                if len(cohort) == 1000:
                    break
            print(f'Activity checked: {checked}; eligible clubs: {len(cohort)}', flush=True)
            if len(cohort) >= 1000:
                break
            if checked >= 4000:
                break
    # No fake clubs or inactive substitutes if discovery falls short.
    if len(cohort) < 500:
        raise RuntimeError(f'Only {len(cohort)} recently active clubs found; trial not started')
    cohort = cohort[:1000 if len(cohort) >= 1000 else 500]
    for i in range(0, len(cohort), 25):
        result = collect.api('/admin/scale/clubs', {'clubs': cohort[i:i+25]})
        if result['registered'] in (500, 1000):
            benchmark(f"registered-{result['registered']}")
    collect.api('/admin/scale/metrics', {'phase': 'cohort', 'metrics': {
        'source': 'EA current-season leaderboards', 'registered': len(cohort),
        'activityWindowDays': 7, 'candidateActivityChecks': checked,
        'activityVerified': True, 'allOneDedicatedAccount': True}})
    with open(os.environ.get('GITHUB_STEP_SUMMARY', '/tmp/scale-summary'), 'a') as f:
        f.write(f'## ClubsKeep capacity trial\n\n{len(cohort)} real leaderboard clubs, each with a league match in the previous 7 days.\n\nSeparate archive: {TRIAL}/#clubs\n\nCollection ends 24 hours after cohort registration starts.\n')
    print('Real cohort registered; 24-hour collection clock started', flush=True)


def segment(minutes):
    deadline = time.monotonic()+minutes*60
    next_metrics = 0
    failures = 0
    s = status()
    if not s['trial']['startedAt']:
        raise RuntimeError('Discovery has not started a real cohort')
    ends_at = s['trial']['endsAt']/1000
    while time.monotonic() < deadline:
        if time.time() >= ends_at:
            benchmark('final-24h')
            print('24-hour trial completed; no new collection claims', flush=True)
            return
        if time.monotonic() >= next_metrics:
            benchmark('collecting')
            next_metrics = time.monotonic()+1800
        try:
            count = collect.run_registered(workers=2, return_count=True)
            failures = 0
            if not count:
                time.sleep(15)
        except Exception:
            failures += 1
            print(f'Trial collector retry {failures}', flush=True)
            if failures >= 5:
                raise RuntimeError('Five consecutive collector failures; review required') from None
            time.sleep(20)
    benchmark('segment-ended')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['discover', 'segment', 'review'])
    parser.add_argument('--minutes', type=int, default=340)
    args = parser.parse_args()
    if args.mode == 'discover':
        discover()
    elif args.mode == 'review':
        benchmark('review')
    else:
        if not 1 <= args.minutes <= 340:
            raise RuntimeError('Segment must be bounded to 340 minutes')
        segment(args.minutes)
