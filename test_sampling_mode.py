import unittest

import numpy as np
import trimesh

from util.utils import sample_mesh


class SamplingModeTests(unittest.TestCase):
    def test_vertex_sampling_preserves_vertex_normals_and_hand_region(self):
        mesh = trimesh.creation.box()
        np.random.seed(7)
        points = sample_mesh(
            mesh, 100, get_normals=True, vertex_sampling=True,
            attn_ratio=0.5, attn_centers=np.array([mesh.vertices[0]]), attn_rel_radius=0.2,
        )
        self.assertEqual(points.shape, (100, 6))
        for point in points:
            indices = np.flatnonzero(np.all(np.isclose(mesh.vertices, point[:3]), axis=1))
            self.assertTrue(any(np.allclose(mesh.vertex_normals[i], point[3:]) for i in indices))
        self.assertGreaterEqual(np.count_nonzero(np.all(np.isclose(points[:, :3], mesh.vertices[0]), axis=1)), 50)

    def test_surface_sampling_does_not_restrict_points_to_vertices(self):
        mesh = trimesh.creation.box()
        np.random.seed(7)
        points = sample_mesh(mesh, 100, get_normals=True)
        self.assertTrue(any(not np.any(np.all(np.isclose(mesh.vertices, p[:3]), axis=1)) for p in points))


if __name__ == "__main__":
    unittest.main()
