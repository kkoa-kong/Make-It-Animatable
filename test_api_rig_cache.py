import json
import multiprocessing
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from api_rig_cache import RigCache, TTL_SECONDS, fingerprint_engine


def _hold_process_lease(directory, input_path, options, now, engine, content, attempted, entered, release, reports):
    cache = RigCache(Path(directory), clock=lambda: now)
    try:
        attempted.set()
        with cache.lease(Path(input_path), options, engine) as lease:
            if not lease.hit:
                lease.path.write_bytes(content)
            reports.put((lease.hit, lease.key, str(lease.path), lease.path.read_bytes()))
            entered.set()
            if not release.wait(20):
                raise RuntimeError("test did not release process lease")
    finally:
        cache.close()


class RigCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.input = self.root / "character.glb"
        self.input.write_bytes(b"model content A")
        self.now = 1000.0
        self.cache = RigCache(self.root / "cache", clock=lambda: self.now)
        self.addCleanup(self.cache.close)
        self.options = {
            "input_no_fingers": False,
            "input_normal": True,
            "input_bw_fix": True,
            "input_restore_global": False,
            "input_reset_to_rest": True,
            "input_rest_pose": "No",
            "input_rest_parts": [],
        }

    def publish(self, content=b"BLENDER base character"):
        with self.cache.lease(self.input, self.options, "engine A") as lease:
            self.assertFalse(lease.hit)
            lease.path.write_bytes(content)
            return lease.key

    def start_process_lease(self, *, engine="engine A", content=b"BLENDER process publication"):
        context = multiprocessing.get_context("spawn")
        attempted = context.Event()
        entered = context.Event()
        release = context.Event()
        reports = context.Queue()
        self.addCleanup(reports.close)
        process = context.Process(
            target=_hold_process_lease,
            args=(
                str(self.cache.directory), str(self.input), self.options, self.now,
                engine, content, attempted, entered, release, reports,
            ),
        )
        process.start()

        def stop_process():
            if process.is_alive():
                release.set()
                process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(5)

        self.addCleanup(stop_process)
        self.assertTrue(attempted.wait(10), "process did not attempt a lease")
        return process, entered, release, reports

    def finish_process(self, process, release):
        release.set()
        process.join(10)
        self.assertFalse(process.is_alive(), "lease process did not exit")
        self.assertEqual(process.exitcode, 0)

    def test_same_content_and_binding_reuses_base_across_motion_and_restart(self):
        self.publish()
        renamed = self.root / "another-name.glb"
        renamed.write_bytes(self.input.read_bytes())
        options = {**self.options, "animation_id": "different-motion", "input_retarget": False, "input_inplace": False}
        with self.cache.lease(renamed, options, "engine A") as lease:
            self.assertTrue(lease.hit)
            self.assertEqual(lease.path.read_bytes(), b"BLENDER base character")
        self.cache.close()
        restarted = RigCache(self.root / "cache", clock=lambda: self.now)
        self.addCleanup(restarted.close)
        with restarted.lease(self.input, self.options, "engine A") as lease:
            self.assertTrue(lease.hit)
            self.assertEqual(lease.path.read_bytes(), b"BLENDER base character")

    def test_preexisting_cache_without_lock_directory_remains_readable(self):
        key = self.publish()
        directory = self.root / "old-cache"
        shutil.copytree(self.cache.directory / key, directory / key)
        self.assertFalse((directory / ".locks").exists())
        cache = RigCache(directory, clock=lambda: self.now)
        self.addCleanup(cache.close)
        with cache.lease(self.input, self.options, "engine A") as lease:
            self.assertTrue(lease.hit)
            self.assertEqual(lease.key, key)
            self.assertEqual(lease.path.read_bytes(), b"BLENDER base character")

    def test_model_binding_and_engine_changes_do_not_reuse_wrong_rig(self):
        original_key = self.publish()
        changed = self.root / "changed.glb"
        changed.write_bytes(b"model content B")
        cases = [(changed, self.options, "engine A"), (self.input, self.options, "engine B")]
        for field, value in {
            "input_no_fingers": True,
            "input_normal": False,
            "input_bw_fix": False,
            "input_restore_global": True,
            "input_reset_to_rest": False,
            "input_rest_pose": "A-pose",
            "input_rest_parts": ["Arms"],
        }.items():
            cases.append((self.input, {**self.options, field: value}, "engine A"))
        for model, options, engine in cases:
            with self.subTest(options=options, engine=engine):
                with self.cache.lease(model, options, engine) as lease:
                    self.assertFalse(lease.hit)
                    self.assertNotEqual(lease.key, original_key)
                    lease.path.write_bytes(b"BLENDER different binding")

    def test_ignored_parts_order_is_not_a_different_binding(self):
        first = {**self.options, "input_rest_parts": ["Legs", "Arms"]}
        with self.cache.lease(self.input, first, "engine A") as lease:
            lease.path.write_bytes(b"BLENDER selected pose parts")
        second = {**self.options, "input_rest_parts": ["Arms", "Legs", "Arms"]}
        with self.cache.lease(self.input, second, "engine A") as lease:
            self.assertTrue(lease.hit)
            self.assertEqual(lease.path.read_bytes(), b"BLENDER selected pose parts")

    def test_fixed_expiry_is_not_extended_by_hits(self):
        self.publish()
        self.now += TTL_SECONDS - 1
        with self.cache.lease(self.input, self.options, "engine A") as lease:
            self.assertTrue(lease.hit)
        self.now += 1
        with self.cache.lease(self.input, self.options, "engine A") as lease:
            self.assertFalse(lease.hit)
            lease.path.write_bytes(b"BLENDER regenerated after expiry")
        with self.cache.lease(self.input, self.options, "engine A") as lease:
            self.assertTrue(lease.hit)
            self.assertEqual(lease.path.read_bytes(), b"BLENDER regenerated after expiry")

    def test_failed_job_does_not_publish_partial_base(self):
        with self.assertRaisesRegex(RuntimeError, "job failed"):
            with self.cache.lease(self.input, self.options, "engine A") as lease:
                partial = lease.path
                lease.path.write_bytes(b"BLENDER incomplete job")
                raise RuntimeError("job failed")
        self.assertFalse(partial.exists())
        with self.cache.lease(self.input, self.options, "engine A") as lease:
            self.assertFalse(lease.hit)
            lease.path.write_bytes(b"BLENDER successful replacement")

    def test_missing_base_cannot_be_reported_as_hit(self):
        self.publish()
        with self.cache.lease(self.input, self.options, "engine A") as lease:
            path = lease.path
        path.unlink()
        with self.cache.lease(self.input, self.options, "engine A") as lease:
            self.assertFalse(lease.hit)
            lease.path.write_bytes(b"BLENDER repaired cache")

    def test_engine_signature_tracks_assets_and_code_contents(self):
        engine = self.root / "engine"
        sources = (
            "app_v2.py", "app_blender.py", "model.py", "models_ae.py",
            "api_rig_cache.py", "api_mesh_input.py", "util/utils.py",
            "util/misc.py", "util/blender_utils.py", "util/dataset_mixamo.py",
            "util/dataset_mixamo_additional.py", "util/Hunyuan3D_21/model.py",
        )
        for name in sources:
            path = engine / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"source version A")
        checkpoints, vae = engine / "weights", engine / "vae"
        checkpoints.mkdir()
        vae.mkdir()
        for name in ("bw_joints.pth", "joints_coarse.pth", "pose.pth"):
            (checkpoints / name).write_bytes(b"weight version A")
        for name in ("config.yaml", "model.fp16.ckpt"):
            (vae / name).write_bytes(b"vae version A")
        template = engine / "bones.fbx"
        template.write_bytes(b"template version A")

        def signature(version="4.3.0"):
            return fingerprint_engine(engine, checkpoints, vae, template, version)

        original = signature()
        self.assertNotEqual(original, signature("4.4.0"))
        for asset in (checkpoints / "pose.pth", vae / "model.fp16.ckpt", template, engine / "app_blender.py"):
            with self.subTest(asset=asset.name):
                content, stat = asset.read_bytes(), asset.stat()
                asset.write_bytes(content.replace(b"A", b"B"))
                os.utime(asset, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                self.assertNotEqual(original, signature())
                asset.write_bytes(content)
                os.utime(asset, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    def test_cleanup_preserves_active_rig_and_unrelated_files(self):
        self.publish()
        unrelated = self.root / "cache" / "user-directory"
        unrelated.mkdir()
        (unrelated / "keep.txt").write_text("not a cache entry")
        with self.cache.lease(self.input, self.options, "engine A") as lease:
            path = lease.path
            self.now += TTL_SECONDS
            self.cache.cleanup()
            self.assertEqual(path.read_bytes(), b"BLENDER base character")
        self.cache.cleanup()
        self.assertFalse(path.exists())
        self.assertEqual((unrelated / "keep.txt").read_text(), "not a cache entry")

    def test_cleanup_preserves_stage_held_by_another_process(self):
        process, entered, release, reports = self.start_process_lease()
        self.assertTrue(entered.wait(10))
        hit, key, raw_path, content = reports.get(timeout=10)
        path = Path(raw_path)
        self.assertFalse(hit)
        self.assertTrue(path.parent.name.startswith(f".stage-{key}-"))
        self.now += TTL_SECONDS
        self.cache.cleanup()
        self.assertEqual(path.read_bytes(), content)
        self.finish_process(process, release)
        self.assertFalse(path.parent.exists())
        self.assertEqual((self.cache.directory / key / "character.blend").read_bytes(), content)

    def test_cleanup_preserves_expired_entry_held_by_another_process(self):
        key = self.publish()
        lock_path = self.cache.directory / ".locks" / f"{key}.lock"
        inode = lock_path.stat().st_ino
        process, entered, release, reports = self.start_process_lease()
        self.assertTrue(entered.wait(10))
        hit, held_key, raw_path, content = reports.get(timeout=10)
        self.assertTrue(hit)
        self.assertEqual(held_key, key)
        self.now += TTL_SECONDS
        self.cache.cleanup()
        self.assertEqual(Path(raw_path).read_bytes(), content)
        self.finish_process(process, release)
        self.cache.cleanup()
        self.assertFalse(Path(raw_path).exists())
        self.assertEqual(lock_path.stat().st_ino, inode)

    def test_shared_key_processes_publish_once(self):
        first, first_entered, first_release, first_reports = self.start_process_lease(content=b"BLENDER first")
        self.assertTrue(first_entered.wait(10))
        first_hit, key, _, first_content = first_reports.get(timeout=10)
        self.assertFalse(first_hit)
        published_at = self.now
        self.now += 1
        second, second_entered, second_release, second_reports = self.start_process_lease(content=b"BLENDER second")
        self.assertFalse(second_entered.wait(0.3), "same-key process entered an active lease")
        self.finish_process(first, first_release)
        self.assertTrue(second_entered.wait(10))
        second_hit, second_key, _, second_content = second_reports.get(timeout=10)
        self.assertTrue(second_hit)
        self.assertEqual(second_key, key)
        self.assertEqual(second_content, first_content)
        self.finish_process(second, second_release)
        entry = self.cache.directory / key
        self.assertEqual((entry / "character.blend").read_bytes(), b"BLENDER first")
        self.assertEqual(json.loads((entry / "metadata.json").read_text())["published_at"], published_at)
        self.assertFalse(any(path.name.startswith(".stage-") for path in self.cache.directory.iterdir()))

    def test_different_key_processes_do_not_block_each_other(self):
        first, first_entered, first_release, first_reports = self.start_process_lease()
        self.assertTrue(first_entered.wait(10))
        _, first_key, first_path, _ = first_reports.get(timeout=10)
        second, second_entered, second_release, second_reports = self.start_process_lease(engine="engine B")
        self.assertTrue(second_entered.wait(10), "different key blocked on the first lease")
        _, second_key, _, _ = second_reports.get(timeout=10)
        self.assertNotEqual(first_key, second_key)
        self.assertTrue(Path(first_path).exists())
        self.finish_process(second, second_release)
        self.finish_process(first, first_release)

    def test_process_death_releases_lock_and_orphan_stage_is_cleanable(self):
        process, entered, _, reports = self.start_process_lease()
        self.assertTrue(entered.wait(10))
        _, key, raw_path, _ = reports.get(timeout=10)
        stage = Path(raw_path).parent
        lock_path = self.cache.directory / ".locks" / f"{key}.lock"
        inode = lock_path.stat().st_ino
        process.terminate()
        process.join(10)
        self.assertFalse(process.is_alive())
        self.assertTrue(stage.exists())
        self.cache.cleanup()
        self.assertFalse(stage.exists())
        self.assertEqual(lock_path.stat().st_ino, inode)
        replacement, replacement_entered, replacement_release, replacement_reports = self.start_process_lease()
        self.assertTrue(replacement_entered.wait(10), "dead process retained its key lock")
        hit, replacement_key, _, _ = replacement_reports.get(timeout=10)
        self.assertFalse(hit)
        self.assertEqual(replacement_key, key)
        self.finish_process(replacement, replacement_release)

    def test_cleanup_does_not_remove_unrelated_contents_or_symlinks(self):
        cache_directory = self.cache.directory
        cache_directory.mkdir()
        unrelated_entry = cache_directory / ("a" * 64)
        unrelated_entry.mkdir()
        (unrelated_entry / "keep.txt").write_text("unrelated entry")
        unrelated_stage = cache_directory / f".stage-{'b' * 64}-{'c' * 32}"
        unrelated_stage.mkdir()
        (unrelated_stage / "keep.txt").write_text("unrelated stage")
        target = self.root / "external"
        target.mkdir()
        (target / "character.blend").write_bytes(b"external rig")
        symlink = cache_directory / ("d" * 64)
        symlink.symlink_to(target, target_is_directory=True)
        self.cache.cleanup()
        self.assertEqual((unrelated_entry / "keep.txt").read_text(), "unrelated entry")
        self.assertEqual((unrelated_stage / "keep.txt").read_text(), "unrelated stage")
        self.assertTrue(symlink.is_symlink())
        self.assertEqual((target / "character.blend").read_bytes(), b"external rig")

    def test_failed_cleaner_start_does_not_prevent_cache_use_or_restart(self):
        with patch("api_rig_cache.threading.Thread.start", side_effect=RuntimeError("cannot start thread")):
            with self.assertLogs("mia-v2-api.rig-cache", level="ERROR"):
                self.assertFalse(self.cache.schedule_cleanup())
        self.publish(b"BLENDER usable after cleaner error")
        self.cache.close()
        restarted = RigCache(self.root / "cache", clock=lambda: self.now)
        self.addCleanup(restarted.close)
        with restarted.lease(self.input, self.options, "engine A") as lease:
            self.assertTrue(lease.hit)
            self.assertEqual(lease.path.read_bytes(), b"BLENDER usable after cleaner error")

    def test_cleanup_hook_is_single_flight_and_joined(self):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        class PausedCleanup(RigCache):
            def cleanup(inner):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test did not release cleaner")
                super().cleanup()
                finished.set()

        cache = PausedCleanup(self.root / "hook-cache", clock=lambda: self.now)
        self.addCleanup(cache.close)
        self.addCleanup(release.set)
        with cache.lease(self.input, self.options, "engine A") as lease:
            lease.path.write_bytes(b"BLENDER expired hook fixture")
            published_key = lease.key
        self.now += TTL_SECONDS
        self.assertTrue(cache.schedule_cleanup())
        self.assertTrue(entered.wait(5))
        self.assertFalse(cache.schedule_cleanup())
        release.set()
        self.assertTrue(finished.wait(5))
        cache.close()
        self.assertFalse((self.root / "hook-cache" / published_key).exists())
        self.assertFalse(cache.schedule_cleanup())


if __name__ == "__main__":
    unittest.main()
