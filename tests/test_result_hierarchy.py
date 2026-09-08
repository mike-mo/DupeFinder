from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dupefinder.models import (
    DuplicateGroup,
    FileRecord,
    ReviewItem,
    ReviewMapping,
)
from dupefinder.result_hierarchy import ResultFolderNode, build_result_hierarchy


class ResultHierarchyTests(unittest.TestCase):
    def _item(
        self,
        root: Path,
        key: str,
        relative_paths: tuple[str, str],
        *,
        size: int = 100_000,
    ) -> ReviewItem:
        files = tuple(
            FileRecord(
                id=index + 1,
                root_path=str(root),
                folder_path=str((root / relative).parent),
                path=str(root / relative),
                size=size,
                mtime_ns=index + 1,
                ctime_ns=index + 1,
                full_hash=key,
            )
            for index, relative in enumerate(relative_paths)
        )
        group = DuplicateGroup(
            id=abs(hash(key)) % 1_000_000,
            root_path=str(root),
            size=size,
            full_hash=key,
            files=files,
        )
        mapping = ReviewMapping(
            group=group,
            relative_path=Path(relative_paths[0]).name,
            paths_by_location=tuple((file.path, file.path) for file in files),
        )
        return ReviewItem(
            key=key,
            fingerprint=key,
            kind="file",
            root_path=str(root),
            label=Path(relative_paths[0]).name,
            relation="file",
            locations=tuple(file.path for file in files),
            mappings=(mapping,),
            size=size,
            savings=size,
            copy_count=2,
            recommended_target=files[0].path,
        )

    def test_lessons_results_form_nested_livepack_and_samples_groups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Ableton"
            items = [
                self._item(
                    root,
                    "banner-1",
                    (
                        r"Lessons\LivePackBanners\L8_library.tif",
                        r"Lessons\LivePackBanners\Archive\L8_library-copy.tif",
                    ),
                ),
                self._item(
                    root,
                    "banner-2",
                    (
                        r"Lessons\LivePackBanners\Loopmaster.tif",
                        r"Lessons\LivePackBanners\Archive\Loopmaster-copy.tif",
                    ),
                ),
                self._item(
                    root,
                    "sample-1",
                    (
                        r"Lessons\Samples\A-Kick.aif",
                        r"Lessons\Samples\Backup\A-Kick-copy.aif",
                    ),
                ),
                self._item(
                    root,
                    "sample-2",
                    (
                        r"Lessons\Samples\A-Snare.aif",
                        r"Lessons\Samples\Backup\A-Snare-copy.aif",
                    ),
                ),
            ]

            hierarchy = build_result_hierarchy(items, root)

            self.assertEqual(len(hierarchy), 1)
            lessons = hierarchy[0]
            self.assertIsInstance(lessons, ResultFolderNode)
            assert isinstance(lessons, ResultFolderNode)
            self.assertEqual(lessons.name, "Lessons")
            self.assertEqual(
                [child.name for child in lessons.children],
                ["LivePackBanners", "Samples"],
            )
            self.assertEqual(lessons.result_count, 4)
            self.assertEqual(lessons.savings, 400_000)


if __name__ == "__main__":
    unittest.main()
