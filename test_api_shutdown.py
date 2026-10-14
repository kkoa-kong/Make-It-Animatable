import subprocess
import sys
import unittest
from pathlib import Path


class NativeShutdownTests(unittest.TestCase):
    def test_registered_retargeting_scripts_exit_cleanly(self):
        # Run in a child: the original failure occurs after Python finalization,
        # not while generating/exporting a character, and would kill the suite.
        code = """
import bpy
from api_worker import unload_blender_scripts
from util.blender_utils import enable_arp

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.object.armature_add()
enable_arp(bpy.context.active_object)
unload_blender_scripts()
"""
        result = subprocess.run(
            [sys.executable, "-X", "faulthandler", "-c", code],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
