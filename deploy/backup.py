"""Root-only streaming encrypted backup and disposable PostgreSQL restore verification."""
import argparse
import json
import os
import secrets
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ROOT = Path('/srv/kamiwa')
BACKUPS = ROOT / 'backups'
KEY = Path('/etc/kamiwa-backup.key')
MAGIC = b'KMWA1'


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def encrypt(source, destination):
    nonce = secrets.token_bytes(12)
    enc = Cipher(algorithms.AES(KEY.read_bytes()), modes.GCM(nonce)).encryptor()
    with open(source, 'rb') as src, open(destination, 'xb') as out:
        os.chmod(destination, 0o600)
        out.write(MAGIC + nonce)
        for chunk in iter(lambda: src.read(1024 * 1024), b''):
            out.write(enc.update(chunk))
        out.write(enc.finalize())
        out.write(enc.tag)


def decrypt(source, destination):
    destination = Path(destination)
    # Authenticate in a private temporary directory before publishing any plaintext.
    with tempfile.TemporaryDirectory(prefix='.kamiwa-decrypt-', dir=destination.parent) as directory:
        os.chmod(directory, 0o700)
        temporary = Path(directory) / 'authenticated.tar.gz'
        with open(source, 'rb') as src, open(temporary, 'xb') as out:
            os.chmod(temporary, 0o600)
            if os.fstat(src.fileno()).st_size < 33 or src.read(5) != MAGIC:
                raise ValueError('Invalid or truncated backup format')
            nonce = src.read(12)
            src.seek(-16, 2)
            tag = src.read(16)
            remaining = src.tell() - 33
            src.seek(17)
            dec = Cipher(algorithms.AES(KEY.read_bytes()), modes.GCM(nonce, tag)).decryptor()
            while remaining:
                chunk = src.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise ValueError('Truncated backup')
                remaining -= len(chunk)
                out.write(dec.update(chunk))
            out.write(dec.finalize())
        os.link(temporary, destination)  # Atomic and never replaces an existing file.


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--decrypt', type=Path)
    parser.add_argument('--output', type=Path)
    options = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Run this backup command as root.')
    if options.decrypt:
        if not options.output or not KEY.exists() or options.output.exists():
            raise SystemExit('Provide --output pointing to a new file; the backup key must exist.')
        temporary_output = options.output.with_name(options.output.name + '.part-' + secrets.token_hex(8))
        try:
            decrypt(options.decrypt, temporary_output)
            # Link without replacing a destination created concurrently.
            os.link(temporary_output, options.output)
        finally:
            temporary_output.unlink(missing_ok=True)
        print(json.dumps({'decrypted': str(options.output), 'authenticated': True}))
        return
    if options.output:
        raise SystemExit('--output is only used with --decrypt.')
    BACKUPS.mkdir(mode=0o700, exist_ok=True)
    BACKUPS.chmod(0o700)
    if not KEY.exists():
        with KEY.open('xb') as output:
            os.chmod(KEY, 0o600)
            output.write(secrets.token_bytes(32))
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    destination = BACKUPS / f'kamiwa-{stamp}.kmw'
    with tempfile.TemporaryDirectory(prefix='kamiwa-backup-') as directory:
        temporary = Path(directory)
        dump = temporary / 'database.dump'
        with dump.open('wb') as output:
            run('runuser', '-u', 'postgres', '--', 'pg_dump', '-Fc', '--no-owner', 'kamiwa', stdout=output)
        archive = temporary / 'backup.tar.gz'
        with tarfile.open(archive, 'w:gz') as output:
            output.add(dump, arcname='database.dump')
            output.add('/etc/kamiwa.env', arcname='environment.env')
            output.add(ROOT / 'requirements.txt', arcname='requirements.txt')
            for name in ('media', 'private_media'):
                output.add(ROOT / name, arcname=name)
        encrypt(archive, destination)
        result = {'backup': str(destination), 'bytes': destination.stat().st_size, 'encrypted': True}
        if options.verify:
            verified = temporary / 'verified.tar.gz'
            decrypt(destination, verified)
            with tarfile.open(verified) as source:
                source.extract('database.dump', path=temporary / 'restored', filter='data')
                result['archive_members'] = len(source.getmembers())
            dbname = 'kamiwa_restorecheck_' + secrets.token_hex(8)
            # Only this newly-created disposable database is dropped.
            run('runuser', '-u', 'postgres', '--', 'createdb', '-O', 'kamiwa', dbname)
            try:
                with (temporary / 'restored' / 'database.dump').open('rb') as data:
                    run('runuser', '-u', 'postgres', '--', 'pg_restore', '--no-owner', '--exit-on-error', '-d', dbname, stdin=data)
                counts = []
                for database in ('kamiwa', dbname):
                    output = run('runuser', '-u', 'postgres', '--', 'psql', '-At', '-d', database, '-c',
                        "SELECT (SELECT count(*) FROM market_account), (SELECT count(*) FROM market_listing), (SELECT count(*) FROM market_payment)", stdout=subprocess.PIPE)
                    counts.append(output.stdout.decode().strip())
                if counts[0] != counts[1]:
                    raise RuntimeError('Restored record counts differ')
                result.update(restore_verified=True, record_counts=counts[0])
            finally:
                run('runuser', '-u', 'postgres', '--', 'dropdb', dbname)
        print(json.dumps(result))


if __name__ == '__main__':
    main()
