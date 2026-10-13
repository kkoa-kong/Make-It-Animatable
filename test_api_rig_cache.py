import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from api_rig_cache import RigCache, TTL_SECONDS, fingerprint_engine


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
