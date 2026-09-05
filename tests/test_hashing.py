from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dupefinder.hashing import full_hash, partial_hash


class HashingTests(unittest.TestCase):
    def test_matching_content_has_matching_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = (b"DupeFinder" * 20_000) + b"tail"
            first = root / "first.bin"
            second = root / "second.bin"
            different = root / "different.bin"
            first.write_bytes(content)
            second.write_bytes(content)
            different.write_bytes(content[:-4] + b"FAIL")

            self.assertEqual(partial_hash(first), partial_hash(second))
            self.assertEqual(full_hash(first), full_hash(second))
            self.assertNotEqual(full_hash(first), full_hash(different))


if __name__ == "__main__":
    unittest.main()
