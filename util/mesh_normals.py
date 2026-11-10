"""Preserve authored vertex normals while flattening and exporting meshes."""

import numpy as np
import trimesh


def load_mesh_with_normals(path):
    """Flatten scene instances without discarding their authored vertex normals."""
    scene = trimesh.load_scene(path)
    meshes = []
    normals = []
    for node in scene.graph.nodes_geometry:
        transform, geometry_name = scene.graph[node]
        source = scene.geometry[geometry_name]
        if not isinstance(source, trimesh.Trimesh):
            continue
        # Normals are covectors: nonuniform scale requires the inverse transpose.
        transformed = np.asarray(source.vertex_normals) @ np.linalg.inv(transform[:3, :3])
        lengths = np.linalg.norm(transformed, axis=1, keepdims=True)
        transformed = np.divide(transformed, lengths, out=np.zeros_like(transformed), where=lengths != 0)
        mesh = source.copy()
        mesh.apply_transform(transform)
        meshes.append(mesh)
        normals.append(transformed)
    if not meshes:
        raise ValueError("Input must contain a mesh")
    combined = trimesh.util.concatenate(meshes)
    combined.vertex_normals = np.concatenate(normals, axis=0)
    return combined


def copy_for_blender(mesh, vertex_normals, *, keep_raw, scaling):
    """Copy mesh, optionally preserving normals through coordinate changes."""
    copied = mesh.copy()
    if not isinstance(copied, trimesh.Trimesh):
        return copied
    normals = None if vertex_normals is None else np.array(vertex_normals, copy=True)
    if not keep_raw:
        vertices = np.array(copied.vertices, copy=True)
        vertices[:, 1], vertices[:, 2] = vertices[:, 2].copy(), -vertices[:, 1].copy()
        copied.vertices = vertices / scaling
        if normals is not None:
            normals[:, 1], normals[:, 2] = normals[:, 2].copy(), -normals[:, 1].copy()
    if normals is not None:
        copied.vertex_normals = normals
    return copied
