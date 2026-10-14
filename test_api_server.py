import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import api_server
from api_animations import AnimationCatalog
from api_config import load_config
from api_jobs import JobStore
from api_supervisor import Supervisor


class PersistedResultAvailabilityTests(unittest.TestCase):
    def test_missing_compute_assets_preserve_public_results_and_reject_submission(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            root = Path(temporary)
            settings = replace(
                load_config(), jobs_dir=root / "jobs", rig_cache_dir=root / "rigs",
                checkpoint_dir=root / "missing-checkpoints", animations_dir=root / "motions",
                hy3dgen_models=root / "missing-vae", ui=False,
            )
            jobs = JobStore(settings.jobs_dir)
            jobs.initialize_schema()
            job = jobs.create({"input_url": "https://example.com/model.glb"})
            claimed = jobs.claim_next("gpu4-worker0", "previous-process")
            self.assertEqual(claimed["job_id"], job["job_id"])
            result = b"persisted-result-from-previous-deployment"
            directory = jobs.directory(job["job_id"])
            (directory / "animatable.glb").write_bytes(result)
            (directory / "manifest.json").write_text(json.dumps({
                "options": {}, "animation": None,
                "artifacts": ["animatable.glb", "manifest.json"],
            }))
            self.assertTrue(jobs.finish(job["job_id"], "previous-process", "succeeded"))
            supervisor = Supervisor(settings, jobs)
            with patch.multiple(
                api_server, _settings=settings, _jobs=jobs, _supervisor=supervisor,
                _catalog=AnimationCatalog(settings.animations_dir), _ui_installed=False,
            ), TestClient(api_server.app) as client:
                self.assertEqual(client.get("/health").status_code, 503)
                info = client.get("/info").json()
                self.assertEqual(info["status"], "error")
                self.assertEqual(info["max_concurrency"], 0)
                self.assertEqual(info["running_jobs"], 0)
                response = client.get(f"/jobs/{job['job_id']}")
                self.assertEqual(response.status_code, 200)
                public = response.json()
                self.assertEqual(set(public), {
                    "job_id", "status", "created_at", "updated_at", "error", "error_code",
                    "status_url", "result_url", "manifest_url", "preview_url",
                })
                self.assertEqual(public["status"], "succeeded")
                self.assertEqual(client.get(public["result_url"]).content, result)
                self.assertEqual(client.get(public["manifest_url"]).json()["options"], {})
                self.assertEqual(client.post("/generate", json={
                    "input_url": "https://example.com/new.glb",
                }).status_code, 503)


if __name__ == "__main__":
    unittest.main()
