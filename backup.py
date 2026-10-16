"""Write a private, consistent export; this script never prints archive contents."""
import gzip
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from collect import api, CLUB, HUB, requests

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


# R2 backups (the hub reports target "r2" once its BACKUPS bucket is bound): each table is cut into chunks of fixed rowid
# ranges, so a chunk whose rows have not changed is byte-identical from day to day and is not uploaded again. Every
# backup still has its own manifest listing all chunks, so any one backup restores on its own.
CHUNK_ROWS = 2000
PAGE_ROWS = 500
CHUNK_LIMIT = 90_000_000


def store(key, backup_id, data=None):
    """Ask the hub about (data=None) or store (bytes) one backup object; the hub never replaces a recent one."""
    headers = {'Authorization': 'Bearer ' + os.environ['BACKUP_TOKEN']}
    url = HUB + '/admin/backup/object?' + urlencode({'key': key, 'backupId': backup_id})
    for attempt in range(3):
        try:
            if data is None:
                response = requests.get(url, headers=headers, impersonate='chrome', timeout=20, allow_redirects=False)
            else:
                response = requests.put(url, data=data, impersonate='chrome', timeout=300, allow_redirects=False,
                    headers={**headers, 'Content-Type': 'application/octet-stream', 'X-Content-SHA256': hashlib.sha256(data).hexdigest()})
            if response.status_code == 200:
                return response.json()
            if response.status_code < 500 or attempt == 2:
                raise RuntimeError(f'Hub backup object: HTTP {response.status_code}')
        except RuntimeError:
            raise
        except Exception:
            if attempt == 2:
                raise RuntimeError('Hub backup object: connection failed') from None
        time.sleep(5)


def seal(plain):
    """age-encrypt bytes in a private temporary folder; only the ciphertext is returned."""
    with tempfile.TemporaryDirectory() as folder:
        source, target = Path(folder) / 'chunk', Path(folder) / 'chunk.age'
        source.write_bytes(plain)
        encrypt(source, target)
        return target.read_bytes()


class Restore:
    """Test restore into a throwaway on-disk database, fed from the exact compressed bytes that get uploaded."""
    def __init__(self, folder, schema):
        self.db = sqlite3.connect(str(Path(folder) / 'restore.sqlite'))
        for sql in schema:
            self.db.execute(sql)
        self.goals = 0
        self.match_ids = []

    def load(self, table, packed):
        if not re.fullmatch(r'[a-z_]+', table):
            raise RuntimeError('Restore check failed: unexpected table name')
        rows = [json.loads(line) for line in gzip.decompress(packed).splitlines()]
        for row in rows:
            if not all(re.fullmatch(r'[a-z_]+', key) for key in row):
                raise RuntimeError('Restore check failed: unexpected column name')
            columns = ','.join('"' + key + '"' for key in row)
            self.db.execute(f'INSERT INTO {table} ({columns}) VALUES ({",".join("?" * len(row))})', list(row.values()))
            if table == 'player_appearances' and row.get('club_id') == CLUB:
                self.goals += json.loads(row['public_json']).get('goals', 0)
            if table == 'matches':
                self.match_ids.append((row['match_id'],))
        return len(rows)

    def check(self, counts):
        if self.db.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('Restore check failed: broken links between tables')
        for table, expected in counts.items():
            if self.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] != expected:
                raise RuntimeError(f'Restore check failed: {table} row count differs')
        if sorted(self.db.execute('SELECT match_id FROM matches').fetchall()) != sorted(self.match_ids):
            raise RuntimeError('Restore check failed: match list differs')
        restored = self.db.execute("SELECT COALESCE(SUM(json_extract(public_json,'$.goals')),0) FROM player_appearances WHERE club_id=?", (CLUB,)).fetchone()[0]
        if restored != self.goals:
            raise RuntimeError('Restore check failed: club goal total differs')
        self.db.close()


def run_r2(start, token):
    backup_id, schema, counts = start['backupId'], start.get('schema'), start.get('counts') or {}
    if not schema:
        raise RuntimeError('R2 backups need the table definitions for their test restore')
    if not re.fullmatch(r'age1[0-9a-z]{58}', AGE_RECIPIENT):
        raise RuntimeError('R2 backups must be encrypted: set BACKUP_AGE_RECIPIENT')
    chunks, packed_bytes, uploaded = [], 0, 0
    with tempfile.TemporaryDirectory() as folder:
        restore = Restore(folder, schema)

        def flush(table, index, lines):
            nonlocal packed_bytes, uploaded
            packed = gzip.compress(b'\n'.join(lines), mtime=0)
            if len(packed) > CHUNK_LIMIT:
                raise RuntimeError(f'Backup chunk of {table} is too large to store')
            # The restore check reads the very bytes that are sealed and uploaded.
            rows = restore.load(table, packed)
            digest = hashlib.sha256(packed).hexdigest()
            key = f'chunks/{table}/{digest}.ndjson.gz.age'
            if not store(key, backup_id)['fresh']:
                sealed = seal(packed)
                if store(key, backup_id, sealed)['state'] == 'stored':
                    uploaded += len(sealed)
            chunks.append({'table': table, 'index': index, 'rows': rows, 'sha256': digest, 'key': key})
            packed_bytes += len(packed)

        try:
            for table in start['tables']:
                index, lines, after, exported = None, [], 0, 0
                while True:
                    page = api('/admin/backup/page', {'backupId': backup_id, 'table': table, 'after': after, 'limit': PAGE_ROWS}, token)
                    for row in page['rows']:
                        at = (row.pop('_cursor') - 1) // CHUNK_ROWS
                        if index is not None and at != index:
                            flush(table, index, lines)
                            lines = []
                        index = at
                        lines.append(json.dumps(row, separators=(',', ':')).encode())
                        exported += 1
                    after = page['next']
                    if after is None:
                        break
                if lines:
                    flush(table, index, lines)
                # Refuse a partial export: every table must match the row count taken under the lease.
                if table in counts and exported != counts[table]:
                    raise RuntimeError(f'Backup incomplete: {table} exported {exported} of {counts[table]} rows')
            restore.check({t: counts[t] for t in start['tables'] if t in counts})
            print('Test restore passed')
            manifest = {'format': 'clubskeep-r2-v1', 'schemaVersion': start['schemaVersion'], 'version': start['version'],
                        'backupId': backup_id, 'createdAt': datetime.now(timezone.utc).isoformat(), 'schema': schema,
                        'counts': counts, 'chunks': chunks}
            name = 'manifests/' + datetime.now(timezone.utc).strftime('%Y-%m-%d') + '-' + backup_id + '.json'
            store(name, backup_id, json.dumps(manifest, separators=(',', ':')).encode())
        finally:
            api('/admin/backup/finish', {'backupId': backup_id}, token)
    matches = counts.get('matches', 0)
    print(f'R2 backup saved: {len(chunks)} chunks, {packed_bytes} compressed bytes, {uploaded} bytes uploaded')
    RESULT.write_text(json.dumps({'matches': matches, 'bytes': packed_bytes, 'restoreVerified': True}))
    warn_storage(start)


def warn_storage(start):
    size, limit = start.get('databaseBytes'), start.get('storageWarnBytes')
    if size is not None:
        print(f'Database size: {size / 1_048_576:.1f} MB')
    # The backup is already written; failing now emails the owner about storage headroom.
    if size and limit and size > limit:
        raise RuntimeError(f'Database is {size / 1_048_576:.0f} MB, above the {limit / 1_048_576:.0f} MB storage warning threshold')


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
    if start.get('target') == 'r2':
        return run_r2(start, token)
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
        raise RuntimeError('Backup exceeds the 25 MB GitHub threshold; bind the R2 backup bucket (docs/STORAGE_PLAN.md)')
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
    warn_storage(start)


if __name__ == '__main__':
    complete() if sys.argv[1:] == ['--complete'] else run()
