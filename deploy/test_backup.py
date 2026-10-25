"""Small isolated tests; never opens deployment keys or production backup files."""
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cryptography.exceptions import InvalidTag
from deploy import backup


class BackupIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        key = self.root / 'test.key'
        key.write_bytes(os.urandom(32))
        mocked = patch.object(backup, 'KEY', key)
        mocked.start()
        self.addCleanup(mocked.stop)
        self.source = self.root / 'plain'
        self.source.write_bytes(os.urandom(1_100_000))
        self.encrypted = self.root / 'backup.kmw'
        backup.encrypt(self.source, self.encrypted)

    def test_roundtrip_and_existing_destination_preserved(self):
        target = self.root / 'restored'
        backup.decrypt(self.encrypted, target)
        self.assertEqual(target.read_bytes(), self.source.read_bytes())
        with self.assertRaises(FileExistsError):
            backup.decrypt(self.encrypted, target)
        self.assertEqual(target.read_bytes(), self.source.read_bytes())

    def test_tampering_never_publishes_plaintext(self):
        data = bytearray(self.encrypted.read_bytes())
        data[100] ^= 1
        self.encrypted.write_bytes(data)
        target = self.root / 'restored'
        with self.assertRaises(InvalidTag):
            backup.decrypt(self.encrypted, target)
        self.assertFalse(target.exists())
        self.assertFalse(list(self.root.glob('.kamiwa-decrypt-*')))

    def test_truncated_header_is_rejected_without_output(self):
        self.encrypted.write_bytes(backup.MAGIC + b'truncated')
        target = self.root / 'restored'
        with self.assertRaises(ValueError):
            backup.decrypt(self.encrypted, target)
        self.assertFalse(target.exists())
