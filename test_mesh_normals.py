import json
import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np
import trimesh

from util.mesh_normals import copy_for_blender, load_mesh_with_normals


class MeshNormalsTests(unittest.TestCase):
    def make_mesh(self):
        # Duplicate positions with deliberately different normals represent a hard edge.
        vertices = [[0, 0, 0], [1, 0, 0], [0, 1, 0],
                    [0, 0, 0], [0, 1, 0], [0, 0, 1]]
        normals = [[0, 0, 1]] * 3 + [[1, 0, 0]] * 3
        return trimesh.Trimesh(vertices=vertices, faces=[[0, 1, 2], [3, 4, 5]],
                               vertex_normals=normals, process=False)

    def test_opt_in_keeps_authored_normals_while_default_does_not(self):
        mesh = self.make_mesh()
        with tempfile.TemporaryDirectory() as tmp:
            off = Path(tmp) / 'off.glb'
            on = Path(tmp) / 'on.glb'
            copy_for_blender(mesh, None, keep_raw=True, scaling=1).export(off)
            copy_for_blender(mesh, mesh.vertex_normals, keep_raw=True, scaling=1).export(on)
            off_bytes = off.read_bytes()
            json_size = struct.unpack_from('<I', off_bytes, 12)[0]
            off_doc = json.loads(off_bytes[20:20 + json_size])
            self.assertNotIn('NORMAL', off_doc['meshes'][0]['primitives'][0]['attributes'])
            on_mesh = next(iter(trimesh.load_scene(on).geometry.values()))
            np.testing.assert_allclose(on_mesh.vertex_normals, mesh.vertex_normals, atol=1e-6)

    def test_scene_transform_and_hard_edges_survive_mesh_flattening(self):
        scene = trimesh.Scene()
        transform = np.array([[0, -2, 0, 2], [1, 0, 0, 3], [0, 0, 3, 4], [0, 0, 0, 1]])
        scene.add_geometry(self.make_mesh(), transform=transform)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'scene.glb'
            path.write_bytes(scene.export(file_type='glb'))
            source = trimesh.load_scene(path)
            mesh = load_mesh_with_normals(path)
            node = source.graph.nodes_geometry[0]
            matrix, name = source.graph[node]
            original = source.geometry[name]
            expected = np.asarray(original.vertex_normals) @ np.linalg.inv(matrix[:3, :3])
            expected /= np.linalg.norm(expected, axis=1, keepdims=True)
            np.testing.assert_allclose(mesh.vertex_normals, expected, atol=1e-6)
            self.assertFalse(np.allclose(mesh.vertex_normals[0], mesh.vertex_normals[3]))

    def test_copy_preserves_normals_after_coordinate_conversion(self):
        mesh = self.make_mesh()
        normals = np.asarray(mesh.vertex_normals).copy()
        for keep_raw in (True, False):
            result = copy_for_blender(mesh, normals, keep_raw=keep_raw, scaling=2)
            expected = normals.copy()
            if not keep_raw:
                expected[:, 1], expected[:, 2] = normals[:, 2], -normals[:, 1]
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'intermediate.glb'
                result.export(path)
                loaded = load_mesh_with_normals(path)
                np.testing.assert_allclose(loaded.vertex_normals, expected, atol=1e-6)
                self.assertFalse(np.allclose(loaded.vertex_normals[0], loaded.vertex_normals[3]))


if __name__ == '__main__':
    unittest.main()
