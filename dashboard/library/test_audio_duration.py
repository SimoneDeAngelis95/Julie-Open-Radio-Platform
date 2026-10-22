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

    def test_initial_unavailable_progress_does_not_discard_final_duration(self):
        from types import SimpleNamespace

        result = SimpleNamespace(
            stdout="out_time_us=N/A\nprogress=continue\nout_time_us=0\nprogress=continue\nout_time_us=3250000\nprogress=end\n",
            stderr='{"input_i":"-20","input_tp":"-3","input_lra":"2"}',
        )
        with patch("library.audio.command", return_value=result):
            self.assertEqual(audio.measure(Path("fixture.mp3"), 100)["duration"], 3.25)

    def test_missing_or_invalid_final_duration_is_rejected(self):
        from types import SimpleNamespace

        for progress in [
            "out_time_us=3000000\nprogress=continue\n",
            "out_time_us=3000000\nprogress=continue\nout_time_us=N/A\nprogress=end\n",
            "out_time_us=0\nprogress=end\n",
            "out_time_us=-1\nprogress=end\n",
        ]:
            with self.subTest(progress=progress):
                result = SimpleNamespace(
                    stdout=progress,
                    stderr='{"input_i":"-20","input_tp":"-3","input_lra":"2"}',
                )
                with (
                    patch("library.audio.command", return_value=result),
                    self.assertRaisesMessage(
                        audio.AudioError,
                        "Durata dell’audio decodificato non disponibile",
                    ),
                ):
                    audio.measure(Path("fixture.mp3"), 100)

    def test_real_vbr_mp3_without_duration_index_is_prepared(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = root / "music.mp3"
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=12",
                    "-c:a",
                    "libmp3lame",
                    "-q:a",
                    "3",
                    "-write_xing",
                    "0",
                    str(original),
                ],
                check=True,
            )
            result = audio.prepare(original, root, -16, -1.5)
            self.assertAlmostEqual(result["duration"], 12, delta=0.1)
            self.assertTrue(result["path"].exists())
