"""The DNS resolver step that runs as root before the server starts.

Under nodo a guest has no resolver at all, so without this step every yt-dlp lookup
fails. The tests write into a temporary directory, never into /etc.
"""

import os
import tempfile
import unittest

import config
import resolver


class TestPlan(unittest.TestCase):
    def test_under_a_node_with_nothing_set_the_defaults_are_used(self):
        self.assertEqual(
            resolver.plan({}, under_node=True), config.DEFAULT_DNS_SERVERS
        )

    def test_outside_a_node_with_nothing_set_nothing_changes(self):
        """Docker already gives the container a resolver. Do not replace it."""
        self.assertIsNone(resolver.plan({}, under_node=False))

    def test_an_explicit_value_wins_everywhere(self):
        for under_node in (True, False):
            with self.subTest(under_node=under_node):
                self.assertEqual(
                    resolver.plan({"YT_DNS_SERVERS": "10.0.0.53"}, under_node),
                    ("10.0.0.53",),
                )

    def test_an_empty_value_is_unset(self):
        self.assertEqual(
            resolver.plan({"YT_DNS_SERVERS": "  "}, under_node=True),
            config.DEFAULT_DNS_SERVERS,
        )


class TestRender(unittest.TestCase):
    def test_one_nameserver_line_per_server_and_short_timeouts(self):
        text = resolver.render(("9.9.9.9", "2620:fe::fe"))
        lines = text.splitlines()
        self.assertIn("nameserver 9.9.9.9", lines)
        self.assertIn("nameserver 2620:fe::fe", lines)
        self.assertIn("options timeout:2 attempts:2", lines)
        self.assertTrue(text.endswith("\n"))

    def test_the_defaults_fit_in_what_glibc_reads(self):
        self.assertLessEqual(len(config.DEFAULT_DNS_SERVERS), config.MAX_DNS_SERVERS)


class TestMain(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "resolv.conf")
        self.node_config = os.path.join(self.dir.name, "__config__")

    def _main(self, env, under_node):
        if under_node:
            with open(self.node_config, "wb") as handle:
                handle.write(b"\x00")
        return resolver.main(env=env, path=self.path, node_config=self.node_config)

    def _read(self):
        with open(self.path, encoding="ascii") as handle:
            return handle.read()

    def test_under_a_node_the_file_is_written(self):
        self.assertEqual(self._main({}, under_node=True), 0)
        for server in config.DEFAULT_DNS_SERVERS:
            self.assertIn(f"nameserver {server}\n", self._read())

    def test_an_empty_file_from_the_image_is_replaced(self):
        open(self.path, "w").close()
        self.assertEqual(self._main({}, under_node=True), 0)
        self.assertIn("nameserver", self._read())

    def test_outside_a_node_an_existing_file_is_left_alone(self):
        with open(self.path, "w") as handle:
            handle.write("nameserver 127.0.0.11\n")
        self.assertEqual(self._main({}, under_node=False), 0)
        self.assertEqual(self._read(), "nameserver 127.0.0.11\n")

    def test_a_symlink_is_replaced_not_written_through(self):
        target = os.path.join(self.dir.name, "target")
        with open(target, "w") as handle:
            handle.write("keep\n")
        os.symlink(target, self.path)
        self.assertEqual(self._main({"YT_DNS_SERVERS": "1.1.1.1"}, under_node=False), 0)
        self.assertFalse(os.path.islink(self.path))
        with open(target) as handle:
            self.assertEqual(handle.read(), "keep\n")

    def test_the_file_is_world_readable(self):
        """The server reads it as uid 10001, through glibc, on every lookup."""
        self._main({}, under_node=True)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o644)

    def test_a_bad_value_is_fatal_and_writes_nothing(self):
        self.assertEqual(
            self._main({"YT_DNS_SERVERS": "dns.example"}, under_node=True), 2
        )
        self.assertFalse(os.path.exists(self.path))

    def test_an_unwritable_path_is_fatal(self):
        code = resolver.main(
            env={"YT_DNS_SERVERS": "1.1.1.1"},
            path=os.path.join(self.dir.name, "missing", "resolv.conf"),
            node_config=self.node_config,
        )
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
