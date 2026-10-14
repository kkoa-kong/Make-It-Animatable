import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from api_config import GPUGroup, Settings, load_config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def config(self, text):
        path = self.root / "service.toml"
        path.write_text(text)
        return path

    def test_toml_paths_and_environment_precedence(self):
        path = self.config('[api]\nport=9090\nui=true\n[storage]\njobs_dir="jobs"\n'
                           '[models]\ncheckpoint_dir="weights"\n'
                           '[[workers.gpu_groups]]\nphysical_gpu=4\ncount=6\n')
        with patch.dict(os.environ, {"PORT": "8082", "MIA_RIG_CACHE_DIR": "relative-cache",
                                     "CUDA_VISIBLE_DEVICES": "1", "GPU_ID": "2"}):
            settings = load_config(path)
        self.assertEqual(settings.port, 8082)
        self.assertTrue(settings.ui)
        self.assertEqual(settings.jobs_dir, self.root / "jobs")
        self.assertEqual(settings.checkpoint_dir, self.root / "weights")
        self.assertEqual(settings.rig_cache_dir, (Path.cwd() / "relative-cache").resolve())
        self.assertEqual(settings.gpu_groups, (GPUGroup(4, 6),))
        self.assertEqual([spec.worker_id for spec in settings.worker_specs()],
                         [f"gpu4-worker{i}" for i in range(6)])

    def test_environment_config_and_gpu_groups_override(self):
        path = self.config('[[workers.gpu_groups]]\nphysical_gpu=4\ncount=6\n')
        groups = [{"physical_gpu": 2, "count": 2}, {"physical_gpu": 5, "count": 1}]
        with patch.dict(os.environ, {"MIA_CONFIG": str(path), "MIA_GPU_GROUPS": json.dumps(groups),
                                     "WORKER_ID": "public-instance"}):
            settings = load_config()
        self.assertEqual(settings.worker_id, "public-instance")
        self.assertEqual(settings.gpu_groups, (GPUGroup(2, 2), GPUGroup(5, 1)))
        self.assertEqual([spec.physical_gpu for spec in settings.worker_specs()], [2, 2, 5])

    def test_explicit_config_path_wins_over_mia_config(self):
        path = self.config('[api]\nport=8088\n')
        with patch.dict(os.environ, {"MIA_CONFIG": str(self.root / "missing.toml")}):
            self.assertEqual(load_config(path).port, 8088)

    def test_legacy_single_gpu_selection(self):
        self.assertEqual(load_config().gpu_groups, (GPUGroup(0, 1),))
        with patch.dict(os.environ, {"CUDA_VISIBLE_DEVICES": "4", "GPU_ID": "0"}):
            self.assertEqual(load_config().gpu_groups, (GPUGroup(4, 1),))
        with patch.dict(os.environ, {"CUDA_VISIBLE_DEVICES": "2,3", "GPU_ID": "5"}):
            self.assertEqual(load_config().gpu_groups, (GPUGroup(5, 1),))

    def test_json_roundtrip_and_frozen_settings(self):
        settings = replace(Settings(), gpu_groups=(GPUGroup(4, 6),), ui=True)
        self.assertEqual(Settings.from_json(settings.to_json()), settings)
        with self.assertRaises(AttributeError):
            settings.port = 1

    def test_invalid_bounds_and_gpu_groups(self):
        for values in ({"port": 0}, {"port": True}, {"cpu_threads_per_worker": 0},
                       {"queue_poll_interval_ms": 0}, {"shutdown_timeout_seconds": float("inf")},
                       {"download_timeout": 0}, {"max_input_bytes": 0}, {"ui": "false"},
                       {"worker_id": "bad/name"}, {"jobs_dir": Path("relative")},
                       {"gpu_groups": ()}, {"gpu_groups": (GPUGroup(4, 1), GPUGroup(4, 2))}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                replace(Settings(), **values)
        for physical_gpu, count in ((-1, 1), (0, 0), (True, 1), (0, 65)):
            with self.subTest(physical_gpu=physical_gpu, count=count), self.assertRaises(ValueError):
                GPUGroup(physical_gpu, count)

    def test_unknown_config_and_malformed_overrides_fail(self):
        with self.assertRaisesRegex(ValueError, "unknown api"):
            load_config(self.config('[api]\npoort=8888\n'))
        for variable, value in (("MIA_UI", "sometimes"), ("PORT", "bad"),
                                ("MIA_GPU_GROUPS", "[]"), ("MIA_JOBS_DIR", "")):
            with self.subTest(variable=variable), patch.dict(os.environ, {variable: value}), self.assertRaises(ValueError):
                load_config()


if __name__ == "__main__":
    unittest.main()
