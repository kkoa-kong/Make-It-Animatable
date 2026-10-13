import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from api_animations import AnimationCatalog, DATASET, REVISION


class AnimationCatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.payload = b"Temporary animation fixture; no FBX parsing is required.\n"
        self.records = [
            self.record(1, "z_exact.fbx", "Walk", "Original description.\n  Keep spacing!"),
            self.record(2, "b_name.fbx", "Walk Forward", "Carry a briefcase."),
            self.record(3, "a_description.fbx", "Idle", "Walk calmly while carrying a briefcase."),
            self.record(4, "d_repeat.fbx", "Walk", "Another source motion with the same display name."),
            self.record(5, "nested/c_combined.fbx", "Carry Briefcase", "Walk forward, with care."),
        ]
        for record in self.records:
            path = self.root / record["relative_path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.payload)
        self.write_index()
        self.catalog = AnimationCatalog(self.root)

    def record(self, number, filename, prompt, description):
        return {
            "motion_id": f"10000000-0000-4000-8000-{number:012d}",
            "filename": filename,
            "prompt": prompt,
            "description": description,
            "frames": 24 + number,
            "relative_path": f"animation_motion/{filename}",
            "sha256": hashlib.sha256(self.payload).hexdigest(),
        }

    def write_index(self, records=None, **overrides):
        index = {
            "schema_version": 1,
            "dataset": DATASET,
            "revision": REVISION,
            "motions": self.records if records is None else records,
        }
        index.update(overrides)
        (self.root / "index.json").write_text(json.dumps(index), encoding="utf-8")

    def filenames(self, result):
        return [item["filename"] for item in result["items"]]

    def test_load_lookup_and_nested_local_path_preserve_source_metadata(self):
        self.assertIsNone(self.catalog.load())
        self.assertEqual(len(self.catalog), 5)
        self.assertEqual(self.catalog.dataset, DATASET)
        self.assertEqual(self.catalog.revision, REVISION)
        for original in self.records:
            with self.subTest(filename=original["filename"]):
                self.assertEqual(self.catalog.get(original["motion_id"]), original)
                self.assertEqual(
                    self.catalog.path(original["motion_id"]),
                    (self.root / original["relative_path"]).resolve(),
                )
        self.assertEqual(set(self.catalog.get(self.records[0]["motion_id"])), set(self.records[0]))

    def test_duplicate_display_names_return_every_motion_without_selection(self):
        self.catalog.load()
        result = self.catalog.search(name=" WALK! ")
        self.assertEqual(result["total"], 2)
        self.assertEqual(self.filenames(result), ["d_repeat.fbx", "z_exact.fbx"])
        self.assertEqual(len({item["motion_id"] for item in result["items"]}), 2)
        self.assertIsNone(self.catalog.get("Walk"))
        with self.assertRaises(KeyError):
            self.catalog.path("Walk")
        self.assertEqual(self.catalog.search(name="Walk", q="spacing")["total"], 1)
        self.assertEqual(self.catalog.search(name="Walk Forward")["total"], 1)

    def test_search_ranks_exact_then_name_then_description_with_stable_ties(self):
        self.catalog.load()
        result = self.catalog.search(q="walk")
        self.assertEqual(result["total"], 5)
        self.assertEqual(
            self.filenames(result),
            ["d_repeat.fbx", "z_exact.fbx", "b_name.fbx", "a_description.fbx", "nested/c_combined.fbx"],
        )
        self.assertEqual(
            self.filenames(self.catalog.search(q="BRIEFCASE walk")),
            ["a_description.fbx", "b_name.fbx", "nested/c_combined.fbx"],
        )
        self.assertEqual(self.catalog.search(q="walk nonexistent")["total"], 0)
        self.assertEqual(self.catalog.search(name="not present")["items"], [])

    def test_normalization_preserves_raw_metadata_and_does_not_translate(self):
        records = copy.deepcopy(self.records)
        records[0]["prompt"] = "ＷＡＬＫ—Forward"
        records[0]["description"] = "A café.\n  Original 中文 description."
        self.write_index(records)
        self.catalog.load()
        result = self.catalog.search(name="walk_forward", q="CAFÉ")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0], records[0])
        self.assertEqual(self.catalog.search(q="translated invented tag")["total"], 0)

    def test_pagination_is_deterministic_independent_of_source_order(self):
        self.catalog.load()
        all_items = self.catalog.search()["items"]
        self.assertEqual(
            [item["filename"] for item in all_items], sorted(record["filename"] for record in self.records)
        )
        self.assertEqual(self.catalog.search(limit=2, offset=1), {
            "total": 5, "limit": 2, "offset": 1, "items": all_items[1:3],
        })
        self.assertEqual(self.catalog.search(limit=2, offset=20)["items"], [])
        ranked = self.catalog.search(q="walk")["items"]
        self.write_index(list(reversed(self.records)))
        self.catalog.load()
        self.assertEqual(self.catalog.search()["items"], all_items)
        self.assertEqual(self.catalog.search(q="walk")["items"], ranked)

    def test_category_groups_preserve_metadata_and_partition_the_library(self):
        records = copy.deepcopy(self.records)
        cases = [
            ("Samba Dancing", "dance"), ("Punching", "combat"), ("Breathing Idle", "idle"),
            ("Unrecognized Motion", "other"), ("Walking", "locomotion"),
        ]
        for record, (prompt, _) in zip(records, cases):
            record["prompt"] = prompt
            record["description"] = ""
        self.write_index(records)
        self.catalog.load()
        categories = self.catalog.categories()
        self.assertEqual(sum(item["count"] for item in categories["items"]), len(records))
        found = []
        for record, (_, category_id) in zip(records, cases):
            self.assertEqual(self.catalog.category(record["motion_id"])["id"], category_id)
            self.assertEqual(self.catalog.get(record["motion_id"]), record)
            result = self.catalog.search(category=category_id)
            self.assertEqual(result["items"], [record])
            found.extend(item["motion_id"] for item in result["items"])
        self.assertEqual(set(found), {record["motion_id"] for record in records})
        with self.assertRaises(ValueError):
            self.catalog.search(category="unknown")

    def test_category_filter_applies_before_pagination_and_keyword_matching(self):
        records = copy.deepcopy(self.records)
        for record in records[:2]:
            record["prompt"] = "Samba"
            record["description"] = "Dancing"
        self.write_index(records)
        self.catalog.load()
        result = self.catalog.search(q="samba", category="dance", limit=1, offset=1)
        expected = sorted(records[:2], key=lambda record: record["filename"])
        self.assertEqual(result, {"total": 2, "limit": 1, "offset": 1, "items": expected[1:]})

    def test_pagination_and_query_argument_validation(self):
        self.catalog.load()
        for arguments in (
            {"limit": 0}, {"limit": -1}, {"limit": True}, {"limit": 2.5}, {"limit": "2"},
            {"offset": -1}, {"offset": False}, {"offset": 1.5}, {"offset": "0"},
            {"q": None}, {"q": 1}, {"name": 1},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.catalog.search(**arguments)

    def test_get_and_search_return_independent_copies(self):
        self.catalog.load()
        identifier = self.records[0]["motion_id"]
        record = self.catalog.get(identifier)
        record["description"] = "mutated"
        record["relative_path"] = "../escape.fbx"
        record["tags"] = ["invented"]
        result = self.catalog.search(name="Walk")
        result["items"][0].clear()
        result["items"].append({"motion_id": "invented"})
        self.assertEqual(self.catalog.get(identifier), self.records[0])
        self.assertEqual(self.catalog.search(name="Walk")["total"], 2)
        self.assertEqual(self.catalog.path(identifier), (self.root / self.records[0]["relative_path"]).resolve())

    def test_unknown_ids_do_not_resolve_to_names_or_other_files(self):
        self.catalog.load()
        for identifier in ("unknown", "", "10000000-0000-4000-8000-000000000999"):
            with self.subTest(identifier=identifier):
                self.assertIsNone(self.catalog.get(identifier))
                with self.assertRaises(KeyError):
                    self.catalog.path(identifier)

    def test_missing_catalog_and_unloaded_access_are_explicit_errors(self):
        for operation in (
            lambda: len(self.catalog), lambda: self.catalog.dataset, lambda: self.catalog.revision,
            lambda: self.catalog.get(self.records[0]["motion_id"]),
            lambda: self.catalog.path(self.records[0]["motion_id"]), lambda: self.catalog.search(),
        ):
            with self.assertRaises(RuntimeError):
                operation()
        (self.root / "index.json").unlink()
        with self.assertRaises(FileNotFoundError):
            self.catalog.load()

    def test_missing_asset_and_failed_reload_do_not_expose_stale_catalog(self):
        self.catalog.load()
        (self.root / self.records[0]["relative_path"]).unlink()
        with self.assertRaises(FileNotFoundError):
            self.catalog.path(self.records[0]["motion_id"])
        with self.assertRaises(FileNotFoundError):
            self.catalog.load()
        with self.assertRaises(RuntimeError):
            self.catalog.search()

    def test_empty_assets_and_directories_fail(self):
        path = self.root / self.records[0]["relative_path"]
        path.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "empty"):
            self.catalog.load()
        path.unlink()
        path.mkdir()
        with self.assertRaises(FileNotFoundError):
            self.catalog.load()

    def test_duplicate_ids_including_uuid_case_aliases_are_rejected(self):
        records = copy.deepcopy(self.records)
        records[0]["motion_id"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        records[1]["motion_id"] = records[0]["motion_id"].upper()
        self.write_index(records)
        with self.assertRaisesRegex(ValueError, "Duplicate motion_id"):
            self.catalog.load()
        records[1]["motion_id"] = records[0]["motion_id"]
        self.write_index(records)
        with self.assertRaisesRegex(ValueError, "Duplicate motion_id"):
            self.catalog.load()

    def test_duplicate_filenames_are_rejected(self):
        records = copy.deepcopy(self.records)
        records[1]["filename"] = records[0]["filename"]
        records[1]["relative_path"] = records[0]["relative_path"]
        self.write_index(records)
        with self.assertRaisesRegex(ValueError, "Duplicate filename"):
            self.catalog.load()

    def test_invalid_ids_and_motion_field_types_are_rejected(self):
        for field, value in (
            ("motion_id", "not-a-uuid"), ("motion_id", "1" * 32), ("motion_id", None),
            ("prompt", []), ("description", None), ("frames", True), ("frames", -1),
            ("frames", 3.5), ("sha256", "bad"), ("sha256", "A" * 64),
            ("relative_path", "animation_motion/another.fbx"),
        ):
            with self.subTest(field=field, value=value):
                records = copy.deepcopy(self.records)
                records[0][field] = value
                self.write_index(records)
                with self.assertRaises(ValueError):
                    self.catalog.load()

    def test_schema_pin_and_no_fictional_metadata_fields(self):
        for overrides in (
            {"schema_version": 2}, {"schema_version": True}, {"dataset": "another/dataset"},
            {"revision": "main"}, {"motions": {}}, {"motions": []}, {"tags": []},
        ):
            with self.subTest(overrides=overrides):
                self.write_index(**overrides)
                with self.assertRaises(ValueError):
                    self.catalog.load()
        for field in ("tags", "fps", "hierarchy", "translation"):
            with self.subTest(field=field):
                records = copy.deepcopy(self.records)
                records[0][field] = "not source metadata"
                self.write_index(records)
                with self.assertRaises(ValueError):
                    self.catalog.load()
        records = copy.deepcopy(self.records)
        del records[0]["description"]
        self.write_index(records)
        with self.assertRaises(ValueError):
            self.catalog.load()
        self.write_index([None])
        with self.assertRaises(ValueError):
            self.catalog.load()

    def test_duplicate_json_fields_and_malformed_json_are_rejected(self):
        path = self.root / "index.json"
        path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Duplicate JSON field"):
            self.catalog.load()
        path.write_text('{"motions":', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.catalog.load()

    def test_unsafe_filenames_and_paths_are_rejected(self):
        for filename in (
            "../outside.fbx", "/outside.fbx", "nested/../../outside.fbx", "./file.fbx",
            "nested//file.fbx", "nested/./file.fbx", "nested\\file.fbx", "C:/file.fbx",
            "bad\x00file.fbx", "bad\nfile.fbx", "file.obj", "",
        ):
            with self.subTest(filename=filename):
                records = copy.deepcopy(self.records)
                records[0]["filename"] = filename
                records[0]["relative_path"] = f"animation_motion/{filename}"
                self.write_index(records)
                with self.assertRaises(ValueError):
                    self.catalog.load()
        for relative_path in (
            "../z_exact.fbx", "animation_motion/../z_exact.fbx", "/animation_motion/z_exact.fbx",
            "other/z_exact.fbx", "animation_motion//z_exact.fbx", "animation_motion\\z_exact.fbx",
        ):
            with self.subTest(relative_path=relative_path):
                records = copy.deepcopy(self.records)
                records[0]["relative_path"] = relative_path
                self.write_index(records)
                with self.assertRaises(ValueError):
                    self.catalog.load()

    def test_symlink_escape_is_rejected_at_load_and_after_load(self):
        outside = self.root / "outside.fbx"
        outside.write_bytes(self.payload)
        asset = self.root / self.records[0]["relative_path"]
        self.catalog.load()
        asset.unlink()
        asset.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.catalog.path(self.records[0]["motion_id"])
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.catalog.load()

    def test_directory_symlink_cannot_redefine_trusted_animation_subtree(self):
        assets = self.root / "animation_motion"
        moved = self.root / "other_assets"
        assets.rename(moved)
        assets.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.catalog.load()

    def test_symlink_within_animation_subtree_remains_valid(self):
        asset = self.root / self.records[0]["relative_path"]
        target = self.root / self.records[1]["relative_path"]
        asset.unlink()
        asset.symlink_to(target)
        self.catalog.load()
        self.assertEqual(self.catalog.path(self.records[0]["motion_id"]), target.resolve())

    def write_translations(self, motions=None, **overrides):
        path = self.root / "motion-index.zh-CN.json"
        index = {
            "schema_version": 1, "dataset": DATASET, "revision": REVISION,
            "motions": motions if motions is not None else [{
                "motion_id": self.records[0]["motion_id"],
                "prompt_zh": "行走", "description_zh": "提着公文包向前行走。",
            }],
        }
        index.update(overrides)
        path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        self.catalog = AnimationCatalog(self.root, translation_path=path)
        return path

    def test_chinese_display_search_preserves_original_source_and_exact_english_name(self):
        self.write_translations()
        self.catalog.load()
        identifier = self.records[0]["motion_id"]
        self.assertEqual(self.catalog.get(identifier), self.records[0])
        self.assertEqual(self.catalog.search(q="公文包")["items"], [self.records[0]])
        self.assertEqual(self.catalog.search(q="行走", name="Walk")["items"], [self.records[0]])
        self.assertEqual(self.catalog.search(name="行走")["total"], 0)
        decorated = self.catalog.display(self.catalog.get(identifier))
        self.assertEqual(decorated["prompt_zh"], "行走")
        self.assertEqual(decorated["description_zh"], "提着公文包向前行走。")
        self.assertEqual({key: decorated[key] for key in self.records[0]}, self.records[0])
        untranslated = self.catalog.display(self.records[1])
        self.assertNotIn("prompt_zh", untranslated)
        self.assertIsNone(untranslated["preview_url"])
        self.assertEqual(untranslated["preview_status"], "missing")

    def test_missing_optional_translation_file_does_not_break_source_catalog(self):
        self.catalog = AnimationCatalog(self.root, translation_path=self.root / "absent.json")
        self.catalog.load()
        self.assertEqual(self.catalog.get(self.records[0]["motion_id"]), self.records[0])
        self.assertEqual(self.catalog.search(q="行走")["total"], 0)

    def test_translation_provenance_unknown_ids_and_duplicate_ids_fail_explicitly(self):
        for overrides in (
            {"schema_version": True}, {"dataset": "other/dataset"}, {"revision": "main"},
            {"motions": {}}, {"extra": "unexpected"},
        ):
            with self.subTest(overrides=overrides):
                self.write_translations(**overrides)
                with self.assertRaises(ValueError):
                    self.catalog.load()
        translated = {
            "motion_id": self.records[0]["motion_id"], "prompt_zh": "行走", "description_zh": "行走。",
        }
        for motions in (
            [dict(translated, motion_id="10000000-0000-4000-8000-000000000999")],
            [translated, translated], [dict(translated, prompt_zh="")],
            [dict(translated, description_zh=None)], [dict(translated, prompt="replaces source")],
        ):
            with self.subTest(motions=motions):
                self.write_translations(motions=motions)
                with self.assertRaises(ValueError):
                    self.catalog.load()
                with self.assertRaises(RuntimeError):
                    self.catalog.search()

    def test_preview_display_uses_only_known_same_origin_local_files(self):
        self.catalog.load()
        record = self.records[0]
        self.assertEqual(self.catalog.display(record)["preview_status"], "missing")
        with self.assertRaises(FileNotFoundError):
            self.catalog.preview_path(record["motion_id"])
        preview = self.root / "previews" / f"{record['motion_id']}.mp4"
        preview.parent.mkdir()
        preview.write_bytes(b"Local preview fixture")
        decorated = self.catalog.display(record)
        self.assertEqual(decorated["preview_url"], f"/animations/{record['motion_id']}/preview")
        self.assertEqual(decorated["preview_status"], "ready")
        self.assertEqual(self.catalog.preview_path(record["motion_id"]), preview)
        self.assertEqual(self.catalog.get(record["motion_id"]), record)
        preview.write_bytes(b"")
        self.assertEqual(self.catalog.display(record)["preview_status"], "missing")
        for identifier in ("../outside", "unknown", "10000000-0000-4000-8000-000000000999"):
            with self.subTest(identifier=identifier), self.assertRaises(KeyError):
                self.catalog.preview_path(identifier)

    def test_preview_symlink_escape_is_missing_and_never_exposed(self):
        self.catalog.load()
        record = self.records[0]
        outside = self.root / "outside.mp4"
        outside.write_bytes(b"Outside fixture")
        previews = self.root / "previews"
        previews.mkdir()
        preview = previews / f"{record['motion_id']}.mp4"
        preview.symlink_to(outside)
        self.assertEqual(self.catalog.display(record)["preview_status"], "missing")
        self.assertIsNone(self.catalog.display(record)["preview_url"])
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.catalog.preview_path(record["motion_id"])
        preview.unlink()
        previews.rmdir()
        moved = self.root / "elsewhere"
        moved.mkdir()
        (moved / preview.name).write_bytes(b"Outside fixture")
        previews.symlink_to(moved, target_is_directory=True)
        self.assertEqual(self.catalog.display(record)["preview_status"], "missing")
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.catalog.preview_path(record["motion_id"])


if __name__ == "__main__":
    unittest.main()
