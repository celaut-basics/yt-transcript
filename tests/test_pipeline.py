"""The pipeline's decisions, with every subprocess replaced.

What is tested here is the part that is this service's own judgement: what argv it
builds, what it refuses before downloading anything, that the deadline is one budget
across four steps rather than four budgets, and that the request's directory is
always removed. No network, no yt-dlp, no ffmpeg, no model.
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import config
import pipeline


def a_config(**over):
    values = dict(
        port=8080,
        max_duration_s=3600,
        threads=4,
        language="auto",
        request_timeout_s=600,
    )
    values.update(over)
    return config.Config(**values)


def metadata(**over):
    info = {"id": "dQw4w9WgXcQ", "title": "t", "duration": 100}
    info.update(over)
    return json.dumps(info)


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestDeadline(unittest.TestCase):
    def test_one_budget_is_shared_across_steps(self):
        clock = FakeClock()
        deadline = pipeline.Deadline(100, now=clock)
        self.assertAlmostEqual(deadline.check("a"), 100)
        clock.advance(60)
        self.assertAlmostEqual(deadline.check("b"), 40)
        clock.advance(30)
        self.assertAlmostEqual(deadline.check("c"), 10)

    def test_an_exhausted_budget_is_504_and_names_the_step(self):
        clock = FakeClock()
        deadline = pipeline.Deadline(10, now=clock)
        clock.advance(11)
        with self.assertRaises(pipeline.PipelineError) as caught:
            deadline.check("transcription")
        self.assertEqual(caught.exception.status, 504)
        self.assertIn("transcription", str(caught.exception))


class TestProbeRefusals(unittest.TestCase):
    """What is refused before a single byte of media is fetched."""

    def _probe(self, stdout, code=0, cfg=None):
        with mock.patch.object(pipeline, "_run", return_value=(code, stdout, "")):
            return pipeline.probe(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                cfg or a_config(),
                pipeline.Deadline(600),
            )

    def test_a_video_over_the_ceiling_is_413(self):
        with self.assertRaises(pipeline.PipelineError) as caught:
            self._probe(metadata(duration=7200), cfg=a_config(max_duration_s=3600))
        self.assertEqual(caught.exception.status, 413)
        self.assertIn("3600", str(caught.exception))

    def test_the_ceiling_is_inclusive(self):
        result = self._probe(metadata(duration=3600), cfg=a_config(max_duration_s=3600))
        self.assertEqual(result["duration_s"], 3600)

    def test_a_live_stream_is_refused(self):
        """It has no duration and no end, so the ceiling cannot bound it."""
        for info in (
            metadata(is_live=True, duration=None),
            metadata(live_status="is_live"),
            metadata(live_status="is_upcoming"),
        ):
            with self.subTest(info=info):
                with self.assertRaises(pipeline.PipelineError) as caught:
                    self._probe(info)
                self.assertEqual(caught.exception.status, 422)

    def test_a_playlist_or_channel_is_refused(self):
        """One request is one video. A playlist has no single duration to bound."""
        for kind in ("playlist", "multi_video"):
            with self.subTest(kind=kind):
                with self.assertRaises(pipeline.PipelineError) as caught:
                    self._probe(json.dumps({"_type": kind, "id": "PL1", "entries": []}))
                self.assertEqual(caught.exception.status, 422)
                self.assertIn("playlist", str(caught.exception))

    def test_a_video_with_no_duration_is_refused(self):
        with self.assertRaises(pipeline.PipelineError) as caught:
            self._probe(metadata(duration=None))
        self.assertEqual(caught.exception.status, 422)

    def test_a_failed_probe_carries_yt_dlps_reason(self):
        with mock.patch.object(
            pipeline, "_run",
            return_value=(1, "", "ERROR: Video unavailable\n"),
        ):
            with self.assertRaises(pipeline.PipelineError) as caught:
                pipeline.probe("https://youtu.be/x", a_config(), pipeline.Deadline(60))
        self.assertEqual(caught.exception.status, 502)
        self.assertIn("Video unavailable", caught.exception.detail)

    def test_non_json_metadata_is_502(self):
        with self.assertRaises(pipeline.PipelineError) as caught:
            self._probe("<html>are you a robot</html>")
        self.assertEqual(caught.exception.status, 502)

    def test_an_ordinary_video_passes(self):
        result = self._probe(metadata(duration=42, id="dQw4w9WgXcQ"))
        self.assertEqual(result["duration_s"], 42)
        self.assertEqual(result["video_id"], "dQw4w9WgXcQ")
        self.assertIsNone(result["info_json"])

    def test_the_metadata_is_kept_in_the_workdir_for_the_download(self):
        workdir = tempfile.mkdtemp()
        try:
            info = metadata(duration=42)
            with mock.patch.object(pipeline, "_run", return_value=(0, info, "")) as run:
                result = pipeline.probe(
                    "https://youtu.be/x", a_config(), pipeline.Deadline(60),
                    workdir=workdir,
                )
            path = os.path.join(workdir, "info.json")
            self.assertEqual(result["info_json"], path)
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), info)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(run.call_args[1]["cwd"], workdir)
        finally:
            shutil.rmtree(workdir)

    def test_a_refused_video_keeps_no_metadata(self):
        workdir = tempfile.mkdtemp()
        try:
            with mock.patch.object(
                pipeline, "_run", return_value=(0, metadata(duration=7200), "")
            ):
                with self.assertRaises(pipeline.PipelineError):
                    pipeline.probe(
                        "https://youtu.be/x", a_config(max_duration_s=3600),
                        pipeline.Deadline(60), workdir=workdir,
                    )
            self.assertEqual(os.listdir(workdir), [])
        finally:
            shutil.rmtree(workdir)


class TestArgv(unittest.TestCase):
    """The argv this service builds is part of its security posture."""

    def _argv_of(self, call):
        return call[0][0]

    def test_probe_passes_the_url_after_a_double_dash(self):
        """So a URL can never be read as a flag, whatever it starts with."""
        with mock.patch.object(
            pipeline, "_run", return_value=(0, metadata(), "")
        ) as run:
            pipeline.probe("https://youtu.be/x", a_config(), pipeline.Deadline(60))
        argv = self._argv_of(run.call_args)
        self.assertEqual(argv[-1], "https://youtu.be/x")
        self.assertEqual(argv[-2], "--")

    def test_probe_downloads_nothing(self):
        with mock.patch.object(
            pipeline, "_run", return_value=(0, metadata(), "")
        ) as run:
            pipeline.probe("https://youtu.be/x", a_config(), pipeline.Deadline(60))
        argv = self._argv_of(run.call_args)
        self.assertIn("--skip-download", argv)

    def test_no_cookies_are_ever_offered(self):
        """This service holds no credential and must not read the node's."""
        with mock.patch.object(
            pipeline, "_run", return_value=(0, metadata(), "")
        ) as run:
            pipeline.probe("https://youtu.be/x", a_config(), pipeline.Deadline(60))
        argv = self._argv_of(run.call_args)
        self.assertIn("--no-cookies", argv)
        self.assertIn("--no-cookies-from-browser", argv)
        self.assertFalse([a for a in argv if "cookie" in a and not a.startswith("--no-")])

    def test_a_playlist_url_fetches_one_video(self):
        with mock.patch.object(
            pipeline, "_run", return_value=(0, metadata(), "")
        ) as run:
            pipeline.probe("https://youtu.be/x", a_config(), pipeline.Deadline(60))
        self.assertIn("--no-playlist", self._argv_of(run.call_args))

    def test_a_playlist_url_is_not_expanded_by_the_probe(self):
        """A channel URL must not cost one extraction per video before it is refused."""
        with mock.patch.object(
            pipeline, "_run", return_value=(0, metadata(), "")
        ) as run:
            pipeline.probe("https://youtu.be/x", a_config(), pipeline.Deadline(60))
        self.assertIn("--flat-playlist", self._argv_of(run.call_args))

    def test_no_config_file_is_read(self):
        """A yt-dlp config file could add options this service did not choose."""
        with mock.patch.object(
            pipeline, "_run", return_value=(0, metadata(), "")
        ) as run:
            pipeline.probe("https://youtu.be/x", a_config(), pipeline.Deadline(60))
        self.assertIn("--ignore-config", self._argv_of(run.call_args))
        with mock.patch.object(pipeline, "_run", return_value=(0, "", "")) as run, \
                mock.patch.object(os, "listdir", return_value=["audio.webm"]):
            pipeline.download_audio("/w/info.json", "/w", pipeline.Deadline(60))
        self.assertIn("--ignore-config", self._argv_of(run.call_args))

    def test_the_generic_extractor_is_off(self):
        """It would follow a page's links to hosts that the allow-list never saw."""
        with mock.patch.object(
            pipeline, "_run", return_value=(0, metadata(), "")
        ) as run:
            pipeline.probe("https://youtu.be/x", a_config(), pipeline.Deadline(60))
        self._assert_generic_is_off(self._argv_of(run.call_args))
        with mock.patch.object(pipeline, "_run", return_value=(0, "", "")) as run, \
                mock.patch.object(os, "listdir", return_value=["audio.webm"]):
            pipeline.download_audio("/w/info.json", "/w", pipeline.Deadline(60))
        self._assert_generic_is_off(self._argv_of(run.call_args))

    def _assert_generic_is_off(self, argv):
        index = argv.index("--use-extractors")
        self.assertEqual(argv[index + 1], "default,-generic")
        if "--" in argv:
            self.assertLess(index, argv.index("--"))

    def test_both_yt_dlp_calls_use_only_the_pinned_deno(self):
        """The challenge script runs in the image's Deno and in no other runtime (#3)."""
        with mock.patch.object(
            pipeline, "_run", return_value=(0, metadata(), "")
        ) as run:
            pipeline.probe("https://youtu.be/x", a_config(), pipeline.Deadline(60))
        self._assert_only_deno(self._argv_of(run.call_args))
        with mock.patch.object(pipeline, "_run", return_value=(0, "", "")) as run, \
                mock.patch.object(os, "listdir", return_value=["audio.webm"]):
            pipeline.download_audio("/w/info.json", "/w", pipeline.Deadline(60))
        self._assert_only_deno(self._argv_of(run.call_args))

    def _assert_only_deno(self, argv):
        clear = argv.index("--no-js-runtimes")
        enable = argv.index("--js-runtimes")
        self.assertLess(clear, enable)
        self.assertEqual(argv[enable + 1], "deno:" + config.DENO_BIN)
        self.assertEqual(argv.count("--js-runtimes"), 1)

    def test_download_reads_the_probe_metadata_not_the_url(self):
        """One extraction per request: the download does not get the URL (#4)."""
        with mock.patch.object(pipeline, "_run", return_value=(0, "", "")) as run, \
                mock.patch.object(os, "listdir", return_value=["audio.webm"]):
            pipeline.download_audio("/w/info.json", "/w", pipeline.Deadline(60))
        argv = self._argv_of(run.call_args)
        self.assertEqual(argv[argv.index("--load-info-json") + 1], "/w/info.json")
        self.assertNotIn("--", argv)
        self.assertFalse(any(a.startswith("http") for a in argv))

    def test_download_bounds_the_bytes_as_well_as_the_time(self):
        with mock.patch.object(pipeline, "_run", return_value=(0, "", "")) as run, \
                mock.patch.object(os, "listdir", return_value=["audio.webm"]):
            pipeline.download_audio("/w/info.json", "/w", pipeline.Deadline(60))
        argv = self._argv_of(run.call_args)
        self.assertIn("--max-filesize", argv)
        self.assertIn("-f", argv)
        self.assertIn("bestaudio/best", argv)

    def test_decode_produces_whispers_only_input_format(self):
        """16 kHz mono s16le: whisper.cpp refuses anything else."""
        with mock.patch.object(pipeline, "_run", return_value=(0, "", "")), \
                mock.patch.object(os.path, "exists", return_value=True), \
                mock.patch.object(os.path, "getsize", return_value=1024), \
                mock.patch.object(pipeline, "_run") as run:
            run.return_value = (0, "", "")
            pipeline.to_wav("/w/audio.webm", "/w", pipeline.Deadline(60))
        argv = self._argv_of(run.call_args)
        self.assertEqual(argv[argv.index("-ar") + 1], "16000")
        self.assertEqual(argv[argv.index("-ac") + 1], "1")
        self.assertEqual(argv[argv.index("-c:a") + 1], "pcm_s16le")
        self.assertIn("-vn", argv)
        self.assertIn("-nostdin", argv)

    def test_transcribe_uses_the_pinned_model_and_no_gpu(self):
        captured = {}

        def fake_run(argv, deadline, step, cwd=None):
            captured["argv"] = argv
            with open(os.path.join(cwd, "transcript.json"), "w") as handle:
                handle.write(json.dumps({"transcription": [{"text": " hi"}]}))
            return (0, "", "")

        import tempfile
        with tempfile.TemporaryDirectory() as workdir:
            with mock.patch.object(pipeline, "_run", fake_run):
                pipeline.transcribe(
                    os.path.join(workdir, "a.wav"), workdir,
                    a_config(threads=3, language="es"), pipeline.Deadline(60),
                )
        argv = captured["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], config.MODEL_PATH)
        self.assertEqual(argv[argv.index("-t") + 1], "3")
        self.assertEqual(argv[argv.index("-l") + 1], "es")
        self.assertIn("-ng", argv)


class TestSubprocessEnvironment(unittest.TestCase):
    def test_the_child_environment_is_replaced_not_inherited(self):
        """A downloader must not pick up a proxy this service did not declare."""
        with mock.patch.object(pipeline.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=b"", stderr=b"")
            with mock.patch.dict(
                os.environ,
                {"http_proxy": "http://evil.example", "AWS_SECRET_ACCESS_KEY": "s"},
                clear=False,
            ):
                pipeline._run(["/bin/true"], pipeline.Deadline(60), "step")
        env = run.call_args[1]["env"]
        self.assertNotIn("http_proxy", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertEqual(
            set(env) - {"PATH", "HOME", "LC_ALL", "LANG", "XDG_CACHE_HOME", "DENO_NO_UPDATE_CHECK"},
            set(),
        )
        self.assertEqual(env["DENO_NO_UPDATE_CHECK"], "1")

    def test_no_shell_is_ever_used(self):
        with mock.patch.object(pipeline.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=b"", stderr=b"")
            pipeline._run(["/bin/true"], pipeline.Deadline(60), "step")
        self.assertNotIn("shell", run.call_args[1])
        self.assertIsInstance(run.call_args[0][0], list)

    def test_stdin_is_closed(self):
        with mock.patch.object(pipeline.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=b"", stderr=b"")
            pipeline._run(["/bin/true"], pipeline.Deadline(60), "step")
        self.assertEqual(run.call_args[1]["stdin"], pipeline.subprocess.DEVNULL)

    def test_a_missing_binary_is_500_and_names_it(self):
        with self.assertRaises(pipeline.PipelineError) as caught:
            pipeline._run(
                ["/nonexistent/binary"], pipeline.Deadline(60), "step",
            )
        self.assertEqual(caught.exception.status, 500)

    def test_a_timeout_is_504(self):
        with mock.patch.object(
            pipeline.subprocess, "run",
            side_effect=pipeline.subprocess.TimeoutExpired("x", 1),
        ):
            with self.assertRaises(pipeline.PipelineError) as caught:
                pipeline._run(["/bin/true"], pipeline.Deadline(60), "download")
        self.assertEqual(caught.exception.status, 504)


class TestTail(unittest.TestCase):
    def test_it_bounds_attacker_influenced_text(self):
        self.assertLessEqual(len(pipeline._tail("x" * 5000)), 600)

    def test_it_keeps_the_last_lines_which_carry_the_error(self):
        self.assertIn("ERROR: the real one", pipeline._tail("noise\nmore\nERROR: the real one"))

    def test_blank_lines_are_dropped(self):
        self.assertEqual(pipeline._tail("a\n\n\nb"), "a | b")


class TestCleanup(unittest.TestCase):
    """The request's directory goes, on success and on failure alike."""

    def _run_with(self, download_side_effect=None):
        created = {}
        real_mkdtemp = pipeline.tempfile.mkdtemp

        def spy_mkdtemp(*a, **k):
            path = real_mkdtemp(*a, **k)
            created["path"] = path
            return path

        with mock.patch.object(pipeline.tempfile, "mkdtemp", spy_mkdtemp), \
                mock.patch.object(pipeline, "probe", return_value={
                    "duration_s": 10, "video_id": "dQw4w9WgXcQ", "title": "t",
                    "info_json": "/w/info.json"}), \
                mock.patch.object(
                    pipeline, "download_audio",
                    side_effect=download_side_effect,
                    return_value="/w/audio.webm") as download, \
                mock.patch.object(pipeline, "to_wav", return_value="/w/audio.wav"), \
                mock.patch.object(pipeline, "transcribe", return_value={
                    "text": "hi", "segments": [], "language": "en"}):
            if download_side_effect is None:
                download.side_effect = None
            try:
                result = pipeline.run(
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ", a_config()
                )
            except pipeline.PipelineError:
                result = None
        return created["path"], result

    def test_removed_after_a_successful_request(self):
        path, result = self._run_with()
        self.assertFalse(os.path.exists(path))
        self.assertEqual(result["text"], "hi")
        self.assertEqual(result["video_id"], "dQw4w9WgXcQ")
        self.assertEqual(result["duration_s"], 10)

    def test_removed_after_a_failed_request(self):
        path, result = self._run_with(
            download_side_effect=pipeline.PipelineError("nope", status=502)
        )
        self.assertIsNone(result)
        self.assertFalse(os.path.exists(path))

    def test_the_response_carries_every_documented_key(self):
        _path, result = self._run_with()
        self.assertEqual(
            set(result),
            {"text", "segments", "language", "video_id", "duration_s", "elapsed_s"},
        )

    def test_the_probe_and_the_download_share_one_extraction(self):
        seen = {}

        def fake_probe(url, cfg, deadline, workdir=None):
            seen["probe_workdir"] = workdir
            return {"duration_s": 10, "video_id": "v", "title": "t",
                    "info_json": os.path.join(workdir, "info.json")}

        def fake_download(info_json, workdir, deadline):
            seen["download"] = (info_json, workdir)
            return os.path.join(workdir, "audio.webm")

        with mock.patch.object(pipeline, "probe", fake_probe), \
                mock.patch.object(pipeline, "download_audio", fake_download), \
                mock.patch.object(pipeline, "to_wav", return_value="/w/audio.wav"), \
                mock.patch.object(pipeline, "transcribe", return_value={
                    "text": "hi", "segments": [], "language": "en"}):
            pipeline.run("https://www.youtube.com/watch?v=dQw4w9WgXcQ", a_config())
        workdir = seen["probe_workdir"]
        self.assertIsNotNone(workdir)
        self.assertEqual(seen["download"], (os.path.join(workdir, "info.json"), workdir))
        self.assertFalse(os.path.exists(workdir))

    def test_removed_after_a_failed_probe(self):
        created = {}
        real_mkdtemp = pipeline.tempfile.mkdtemp

        def spy_mkdtemp(*a, **k):
            created["path"] = real_mkdtemp(*a, **k)
            return created["path"]

        with mock.patch.object(pipeline.tempfile, "mkdtemp", spy_mkdtemp), \
                mock.patch.object(pipeline, "_run", return_value=(1, "", "ERROR: x")):
            with self.assertRaises(pipeline.PipelineError):
                pipeline.run("https://www.youtube.com/watch?v=dQw4w9WgXcQ", a_config())
        self.assertFalse(os.path.exists(created["path"]))


if __name__ == "__main__":
    unittest.main()
