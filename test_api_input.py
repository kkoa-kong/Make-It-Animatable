import io
import json
import struct
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException
from starlette.datastructures import UploadFile
from starlette.requests import Request

from api_input import input_payload, save_fbx, save_glb
from api_jobs import JobStore


def request(body, content_type, content_length=None):
    chunks = iter([body[:10], body[10:]])

    async def receive():
        try:
            chunk = next(chunks)
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.request", "body": chunk, "more_body": True}

    headers = [(b"content-type", content_type.encode())]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    return Request({"type": "http", "method": "POST", "path": "/generate", "headers": headers}, receive)


class InputTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunked_oversize_upload_is_rejected_before_parsing_completes(self):
        body = b'--abc\r\nContent-Disposition: form-data; name="input_file"; filename="model.glb"\r\n\r\n' + b"x" * (65536 + 65)
        with self.assertRaises(HTTPException) as error:
            async with input_payload(request(body, "multipart/form-data; boundary=abc"), 32):
                self.fail("oversize request was accepted")
        self.assertEqual(error.exception.status_code, 413)

    async def test_json_array_is_not_generation_options(self):
        with self.assertRaises(HTTPException) as error:
            async with input_payload(request(b"[]", "application/json"), 1024):
                self.fail("non-object JSON was accepted")
        self.assertEqual(error.exception.status_code, 422)

    async def test_duplicate_multipart_options_are_rejected(self):
        parts = [b'--abc\r\nContent-Disposition: form-data; name="options"\r\n\r\n{}\r\n'] * 2
        body = b"".join(parts) + b"--abc--\r\n"
        with self.assertRaises(HTTPException) as error:
            async with input_payload(request(body, "multipart/form-data; boundary=abc"), 1024):
                self.fail("duplicate options were accepted")
        self.assertIn(error.exception.status_code, (400, 422))

    async def test_multipart_glb_and_json_options_preserve_bytes(self):
        payload = b"glTF" + struct.pack("<II", 2, 16) + b"abcd"
        body = (
            b'--abc\r\nContent-Disposition: form-data; name="input_file"; filename="character.glb"\r\nContent-Type: model/gltf-binary\r\n\r\n'
            + payload
            + b'\r\n--abc\r\nContent-Disposition: form-data; name="options"\r\n\r\n{"input_inplace": false}\r\n--abc--\r\n'
        )
        async with input_payload(request(body, "multipart/form-data; boundary=abc"), 1024) as (options, upload, animation):
            self.assertEqual(options, {"input_inplace": False})
            self.assertEqual(await upload.read(), payload)
            self.assertIsNone(animation)
        self.assertTrue(upload.file.closed)


class UploadPersistenceTests(unittest.TestCase):
    def test_bad_glb_length_does_not_leave_a_staging_file(self):
        with tempfile.TemporaryDirectory() as temp:
            upload = UploadFile(io.BytesIO(b"glTF" + struct.pack("<II", 2, 99)), size=12, filename="model.glb")
            with self.assertRaises(HTTPException) as error:
                save_glb(upload, 1024, Path(temp))
            self.assertEqual(error.exception.status_code, 422)
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_queued_upload_survives_store_recreation_with_exact_file_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = JobStore(root)
            store.initialize()
            contents = b"glTF" + struct.pack("<II", 2, 16) + b"abcd"
            upload = UploadFile(io.BytesIO(contents), size=len(contents), filename="../../model.glb")
            staged = save_glb(upload, 1024, root)
            animation_bytes = b"Kaydara FBX Binary  \x00\x1a\x00" + struct.pack("<I", 7400) + b"animation data"
            animation_upload = UploadFile(io.BytesIO(animation_bytes), size=len(animation_bytes), filename="move.fbx")
            animation_staged = save_fbx(animation_upload, 1024, root)
            job = store.create(
                {"input_url": None, "_uploaded_input": True, "_uploaded_animation": True},
                input_file=staged,
                animation_file=animation_staged,
            )
            self.assertFalse(staged.exists())
            recreated = JobStore(root)
            recreated.initialize()
            self.assertEqual(recreated.pending(), [job["job_id"]])
            self.assertEqual((recreated.directory(job["job_id"]) / "character.glb").read_bytes(), contents)
            self.assertEqual((recreated.directory(job["job_id"]) / "animation.fbx").read_bytes(), animation_bytes)
            self.assertTrue(recreated.get(job["job_id"])["request"]["_uploaded_input"])


if __name__ == "__main__":
    unittest.main()
