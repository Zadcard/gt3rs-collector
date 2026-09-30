"""Write a private, consistent export; this script never prints archive contents."""
import gzip
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from collect import api


def run():
    token = os.environ['BACKUP_TOKEN']
    start = None
    for attempt in range(12):
        try:
            start = api('/admin/backup/start', {}, token, retries=0)
            break
        except RuntimeError as error:
            if 'HTTP 409' not in str(error) or attempt == 11:
                raise
            time.sleep(10)
    backup_id = start['backupId']
    export = {'schemaVersion': start['schemaVersion'], 'version': start['version'],
              'createdAt': datetime.now(timezone.utc).isoformat(), 'tables': {}}
    try:
        for table in start['tables']:
            rows, after = [], 0
            while True:
                page = api('/admin/backup/page', {'backupId': backup_id, 'table': table, 'after': after}, token)
                for row in page['rows']:
                    row.pop('_cursor', None)
                    rows.append(row)
                after = page['next']
                if after is None:
                    break
            export['tables'][table] = rows
    finally:
        api('/admin/backup/finish', {'backupId': backup_id}, token)
    data = json.dumps(export, separators=(',', ':')).encode()
    out = Path(os.environ.get('BACKUP_DIR', 'backups'))
    out.mkdir(parents=True, exist_ok=True)
    name = 'gt3rs-' + datetime.now(timezone.utc).strftime('%Y-%m-%d') + '.json.gz'
    compressed = gzip.compress(data, mtime=0)
    if len(compressed) > 25_000_000:
        raise RuntimeError('Backup exceeds 25 MB safety threshold; review storage before upload')
    target = out / name
    temp = target.with_suffix('.tmp')
    temp.write_bytes(compressed)
    temp.replace(target)
    target.with_suffix('.sha256').write_text(hashlib.sha256(compressed).hexdigest() + '\n')
    for old in sorted(out.glob('gt3rs-????-??-??.json.gz'))[:-30]:
        old.unlink()
        old.with_suffix('.sha256').unlink(missing_ok=True)
    print(f'Private backup ready: {len(export["tables"]["matches"])} matches; {len(compressed)} compressed bytes')
    size, limit = start.get('databaseBytes'), start.get('storageWarnBytes')
    if size is not None:
        print(f'Database size: {size / 1_048_576:.1f} MB')
    # The backup is already written; failing now emails the owner about quota headroom.
    if size and limit and size > limit:
        raise RuntimeError(f'Database is {size / 1_048_576:.0f} MB, above the {limit / 1_048_576:.0f} MB free-tier warning threshold')


if __name__ == '__main__':
    run()
