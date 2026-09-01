from __future__ import annotations

import tempfile
from pathlib import Path
import subprocess
import unittest
from unittest import mock

import sensevoice_transcription_server as server


class SenseVoiceBridgeSafetyTests(unittest.TestCase):
    def test_content_type_confusion_and_multiple_audio_files_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "invalid_multipart"):
            server._parse_multipart_form("application/x-multipart/form-data; boundary=x", b"")

        boundary = "test-boundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="audio"; filename="one.wav"\r\n\r\n'
            "one\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="audio"; filename="two.wav"\r\n\r\n'
            "two\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")
        with self.assertRaisesRegex(ValueError, "exactly_one_audio_file_required"):
            server._parse_multipart_form(f"multipart/form-data; boundary={boundary}", body)

    def test_subprocess_output_is_not_returned_as_error(self):
        completed = subprocess.CompletedProcess(
            args=["transcribe"],
            returncode=1,
            stdout="meeting body",
            stderr="/private/path and model details",
        )
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            server.subprocess, "run", return_value=completed
        ):
            ok, text, error = server._run_transcribe(["transcribe"], Path(temp_dir), "input")
        self.assertFalse(ok)
        self.assertEqual(text, "")
        self.assertEqual(error, "transcription_process_failed")

    def test_success_without_transcript_fails_closed(self):
        completed = subprocess.CompletedProcess(
            args=["transcribe"],
            returncode=0,
            stdout="",
            stderr="",
        )
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            server.subprocess, "run", return_value=completed
        ):
            ok, text, error = server._run_transcribe(["transcribe"], Path(temp_dir), "input")
        self.assertFalse(ok)
        self.assertEqual(text, "")
        self.assertEqual(error, "transcription_output_missing")

    def test_health_model_status_contains_no_paths(self):
        status = server._model_cache_status()
        self.assertNotIn("cache_root", status)
        self.assertNotIn("python", status)
        self.assertNotIn("path", str(status))


if __name__ == "__main__":
    unittest.main()
