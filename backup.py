"""Write a private, consistent export; this script never prints archive contents."""
import gzip
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from collect import api, CLUB

RESULT = Path(os.environ.get('BACKUP_RESULT', 'backup-result.json'))
# Public age key (age1...). When set, each backup is encrypted to it before it leaves this runner, so the backup
# repository holds no readable account data (emails, password hashes). The matching private key stays offline.
AGE_RECIPIENT = os.environ.get('BACKUP_AGE_RECIPIENT', '').strip()


def encrypt(plain, target):
    if not re.fullmatch(r'age1[0-9a-z]{58}', AGE_RECIPIENT):
        raise RuntimeError('BACKUP_AGE_RECIPIENT is not a valid age public key')
    subprocess.run(['age', '--encrypt', '--recipient', AGE_RECIPIENT, '--output', str(target), str(plain)], check=True)
    if not target.read_bytes().startswith(b'age-encryption.org/v1\n'):
        raise RuntimeError('Encrypted backup failed its format check')


def verify_restore(schema, tables):
    """Restore the export into a throwaway in-memory database using the hub's own table definitions and check it
    matches: every row loads, foreign keys hold, and the club's match list and goal total are unchanged."""
    db = sqlite3.connect(':memory:')
    for sql in schema:
        db.execute(sql)
    for table, rows in tables.items():
        for row in rows:
            if not all(re.fullmatch(r'[a-z_]+', key) for key in row) or not re.fullmatch(r'[a-z_]+', table):
                raise RuntimeError('Restore check failed: unexpected column name')
            columns = ','.join('"' + key + '"' for key in row)
            db.execute(f'INSERT INTO {table} ({columns}) VALUES ({",".join("?" * len(row))})', list(row.values()))
    if db.execute('PRAGMA foreign_key_check').fetchall():
        raise RuntimeError('Restore check failed: broken links between tables')
    for table, rows in tables.items():
        if db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] != len(rows):
            raise RuntimeError(f'Restore check failed: {table} row count differs')
    matches = tables.get('matches', [])
    if sorted(db.execute('SELECT match_id FROM matches').fetchall()) != sorted((r['match_id'],) for r in matches):
        raise RuntimeError('Restore check failed: match list differs')
    goals = sum(json.loads(r['public_json']).get('goals', 0) for r in tables.get('player_appearances', []) if r.get('club_id') == CLUB)
    restored = db.execute("SELECT COALESCE(SUM(json_extract(public_json,'$.goals')),0) FROM player_appearances WHERE club_id=?", (CLUB,)).fetchone()[0]
    if restored != goals:
        raise RuntimeError('Restore check failed: club goal total differs')
    db.close()


def complete():
    """Tell the hub the backup is saved. Runs only after the private repository push succeeded."""
    result = json.loads(RESULT.read_text())
    try:
        api('/admin/backup/complete', result, os.environ['BACKUP_TOKEN'])
    except RuntimeError as error:
        # A hub that predates completion tracking answers 404; the backup itself is already safe.
        if 'HTTP 404' not in str(error):
            raise
        print('Hub does not record backup completion yet')
        return
    print('Backup completion recorded')


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
    # Refuse to save a partial export: every table must match the row count taken under the lease.
    for table, expected in (start.get('counts') or {}).items():
        got = len(export['tables'].get(table, []))
        if got != expected:
            raise RuntimeError(f'Backup incomplete: {table} exported {got} of {expected} rows')
    schema = start.get('schema')
    if schema:
        verify_restore(schema, export['tables'])
        export['schema'] = schema
        print('Test restore passed')
    else:
        print('Test restore skipped: the hub did not send table definitions')
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
    # Read the file back before it replaces yesterday's: it must decompress to exactly this export.
    if gzip.decompress(temp.read_bytes()) != data:
        temp.unlink()
        raise RuntimeError('Backup file failed read-back verification')
    if AGE_RECIPIENT:
        sealed = target.with_name(name + '.age')
        encrypt(temp, sealed)
        temp.unlink()
        target = sealed
        # Once backups are encrypted, readable copies are removed from the working tree too.
        for plain in out.glob('gt3rs-????-??-??.json.gz'):
            plain.unlink()
            plain.with_suffix('.sha256').unlink(missing_ok=True)
    else:
        print('WARNING: BACKUP_AGE_RECIPIENT is not set; this backup is not encrypted')
        temp.replace(target)
    target.with_suffix('.sha256').write_text(hashlib.sha256(target.read_bytes()).hexdigest() + '\n')
    saved = sorted(list(out.glob('gt3rs-????-??-??.json.gz')) + list(out.glob('gt3rs-????-??-??.json.gz.age')), key=lambda p: p.name[:16])
    for old in saved[:-30]:
        old.unlink()
        old.with_suffix('.sha256').unlink(missing_ok=True)
    print(f'Private backup ready: {len(export["tables"]["matches"])} matches; {len(compressed)} compressed bytes')
    RESULT.write_text(json.dumps({'matches': len(export['tables']['matches']), 'bytes': len(compressed), 'restoreVerified': bool(schema)}))
    size, limit = start.get('databaseBytes'), start.get('storageWarnBytes')
    if size is not None:
        print(f'Database size: {size / 1_048_576:.1f} MB')
    # The backup is already written; failing now emails the owner about quota headroom.
    if size and limit and size > limit:
        raise RuntimeError(f'Database is {size / 1_048_576:.0f} MB, above the {limit / 1_048_576:.0f} MB free-tier warning threshold')


if __name__ == '__main__':
    complete() if sys.argv[1:] == ['--complete'] else run()
