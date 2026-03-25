from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from epub2yaml.domain.models import Chapter
from epub2yaml.domain.services import build_batches, dump_yaml_document, merge_delta_package, parse_delta_yaml


class DomainServicesTests(unittest.TestCase):
    def test_build_batches_groups_chapters_by_budget(self) -> None:
        chapters = [
            Chapter(
                index=0,
                title="第一章",
                source_href="chapter1.xhtml",
                content_text="a" * 20,
                content_hash="hash-1",
                estimated_tokens=10,
            ),
            Chapter(
                index=1,
                title="第二章",
                source_href="chapter2.xhtml",
                content_text="b" * 20,
                content_hash="hash-2",
                estimated_tokens=12,
            ),
            Chapter(
                index=2,
                title="第三章",
                source_href="chapter3.xhtml",
                content_text="c" * 20,
                content_hash="hash-3",
                estimated_tokens=8,
            ),
        ]

        batches = build_batches(
            chapters,
            target_input_tokens=20,
            max_input_tokens=25,
            min_chapters_per_batch=1,
            max_chapters_per_batch=3,
            batch_number_start=3,
        )

        self.assertEqual(2, len(batches))
        self.assertEqual("0003", batches[0].batch_id)
        self.assertEqual([0, 1], batches[0].chapter_indices)
        self.assertEqual(22, batches[0].estimated_input_tokens)
        self.assertEqual("0004", batches[1].batch_id)
        self.assertEqual([2], batches[1].chapter_indices)

    def test_parse_delta_yaml_accepts_nested_delta_payload(self) -> None:
        package = parse_delta_yaml(
            """
            delta:
              actors:
                Alice:
                  profile:
                    role: hero
              worldinfo:
                Academy:
                  content: magic school
            """
        )

        self.assertEqual("hero", package.actors["Alice"]["profile"]["role"])
        self.assertEqual("magic school", package.worldinfo["Academy"]["content"])

    def test_parse_delta_yaml_rejects_invalid_root(self) -> None:
        with self.assertRaisesRegex(ValueError, "根节点必须是映射"):
            parse_delta_yaml("- invalid")

    def test_merge_delta_package_merges_nested_dict_and_replaces_list(self) -> None:
        actors_current = {
            "Alice": {
                "profile": {
                    "likes": ["tea"],
                    "goal": "study",
                }
            }
        }
        worldinfo_current = {
            "Academy": {
                "tags": ["school"],
                "content": "old",
            }
        }
        package = parse_delta_yaml(
            """
            delta:
              actors:
                Alice:
                  profile:
                    goal: protect sister
                    likes:
                      - coffee
              worldinfo:
                Academy:
                  content: new
            """
        )

        merged_actors, merged_worldinfo = merge_delta_package(actors_current, worldinfo_current, package)

        self.assertEqual("protect sister", merged_actors["Alice"]["profile"]["goal"])
        self.assertEqual(["coffee"], merged_actors["Alice"]["profile"]["likes"])
        self.assertEqual("new", merged_worldinfo["Academy"]["content"])
        self.assertEqual(["school"], merged_worldinfo["Academy"]["tags"])

    def test_dump_yaml_document_contains_root_key(self) -> None:
        document = dump_yaml_document("actors", {"Alice": {"role": "hero"}})

        self.assertIn("actors:", document)
        self.assertIn("Alice:", document)
        self.assertIn("role: hero", document)


if __name__ == "__main__":
    unittest.main()
