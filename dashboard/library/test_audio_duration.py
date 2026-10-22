import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from . import audio


class AudioDurationTests(SimpleTestCase):
    def test_inaccurate_container_duration_does_not_reject_complete_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = root / "podcast.wav"
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=3",
                    str(original),
                ],
                check=True,
            )
            real_probe = audio.probe

            def inaccurate_probe(path, deadline):
                info = real_probe(path, deadline)
                if path == original:
                    info["format"]["duration"] = "15.0"
                return info

            with patch("library.audio.probe", side_effect=inaccurate_probe):
                result = audio.prepare(original, root, -20, -1.5)
            self.assertAlmostEqual(result["duration"], 3, delta=0.1)
            self.assertTrue(result["path"].exists())

    def test_actual_truncation_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = root / "podcast.wav"
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=3",
                    str(original),
                ],
                check=True,
            )
            real_probe = audio.probe

            def truncated_probe(path, deadline):
                info = real_probe(path, deadline)
                if path != original:
                    info["format"]["duration"] = "1.0"
                return info

            with (
                patch("library.audio.probe", side_effect=truncated_probe),
                self.assertRaisesMessage(audio.AudioError, "La durata è cambiata"),
            ):
                audio.prepare(original, root, -20, -1.5)
