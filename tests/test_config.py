"""The environment contract from `<arch>/.service/service.json`'s `envs`, exercised.

Every value is passed in explicitly rather than read from `os.environ`, so these
tests cannot be changed by the shell that runs them.
"""

import unittest

import config


def load(**env):
    return config.load(env=env, cpu_count=lambda: 4)


class TestDefaults(unittest.TestCase):
    def test_an_empty_environment_is_valid(self):
        """The service must start with nothing set. A node may pass no envs at all."""
        cfg = load()
        self.assertEqual(cfg.port, 8080)
        self.assertEqual(cfg.max_duration_s, 3600)
        self.assertEqual(cfg.language, "auto")

    def test_the_default_port_is_the_declared_api_port(self):
        """8080 here and 8080 in service.json's `api[0].port` are one fact.

        If they drift, the node publishes a port nothing listens on.
        """
        self.assertEqual(load().port, 8080)

    def test_threads_default_to_the_cpus_the_instance_has(self):
        self.assertEqual(config.load(env={}, cpu_count=lambda: 2).threads, 2)
        self.assertEqual(config.load(env={}, cpu_count=lambda: 16).threads, 16)

    def test_threads_never_fall_below_one(self):
        """`os.cpu_count()` can return None or 0; `-t 0` would be passed to whisper."""
        self.assertEqual(config.load(env={}, cpu_count=lambda: 0).threads, 1)

    def test_the_request_timeout_follows_the_duration_ceiling(self):
        """4x, so the timeout cannot be tighter than the work the ceiling allows."""
        self.assertEqual(load().request_timeout_s, 3600 * 4)
        self.assertEqual(load(YT_MAX_DURATION_S="600").request_timeout_s, 2400)

    def test_an_explicit_timeout_wins_over_the_derived_one(self):
        cfg = load(YT_MAX_DURATION_S="600", YT_REQUEST_TIMEOUT_S="90")
        self.assertEqual(cfg.request_timeout_s, 90)

    def test_empty_strings_are_treated_as_unset(self):
        """A node that exports a declared-but-unconfigured env passes "" not absence."""
        cfg = load(YT_PORT="", YT_MAX_DURATION_S="", YT_LANGUAGE="",
                   YT_WHISPER_THREADS="", YT_REQUEST_TIMEOUT_S="")
        self.assertEqual(cfg.port, 8080)
        self.assertEqual(cfg.max_duration_s, 3600)
        self.assertEqual(cfg.language, "auto")
        self.assertEqual(cfg.threads, 4)

    def test_surrounding_whitespace_is_tolerated(self):
        self.assertEqual(load(YT_PORT=" 9000 ").port, 9000)
        self.assertEqual(load(YT_LANGUAGE=" EN ").language, "en")


class TestAccepted(unittest.TestCase):
    def test_values_are_taken(self):
        cfg = load(
            YT_PORT="9000",
            YT_MAX_DURATION_S="600",
            YT_WHISPER_THREADS="2",
            YT_LANGUAGE="es",
        )
        self.assertEqual(cfg.port, 9000)
        self.assertEqual(cfg.max_duration_s, 600)
        self.assertEqual(cfg.threads, 2)
        self.assertEqual(cfg.language, "es")

    def test_language_is_lowercased(self):
        self.assertEqual(load(YT_LANGUAGE="ES").language, "es")

    def test_hyphenated_language_codes(self):
        self.assertEqual(load(YT_LANGUAGE="zh-tw").language, "zh-tw")

    def test_the_model_path_is_not_configurable(self):
        """It is part of the content-addressed filesystem, not of the environment.

        A settable model path would be a way to run a model this spec did not pin.
        """
        cfg = load(YT_MODEL="/somewhere/else.bin", YT_MODEL_PATH="/x")
        self.assertEqual(cfg.model_path, config.MODEL_PATH)


class TestRefused(unittest.TestCase):
    """Refused loudly, never clamped.

    A limit that silently becomes its default when mistyped is not the limit the
    spec declares -- and this one is what stands between a clip and an eight-hour
    livestream on a 4 GB disk.
    """

    def _refused(self, because, **env):
        with self.subTest(because=because, env=env):
            with self.assertRaises(config.ConfigError):
                load(**env)

    def test_non_numeric(self):
        self._refused("not a number", YT_MAX_DURATION_S="abc")
        self._refused("a float", YT_MAX_DURATION_S="60.5")
        self._refused("hex is not decimal", YT_PORT="0x50")
        self._refused("an expression", YT_WHISPER_THREADS="2+2")
        self._refused("a unit suffix", YT_MAX_DURATION_S="60s")

    def test_out_of_range_durations(self):
        self._refused("zero", YT_MAX_DURATION_S="0")
        self._refused("negative", YT_MAX_DURATION_S="-1")
        self._refused("over a day", YT_MAX_DURATION_S="86401")

    def test_out_of_range_ports(self):
        self._refused("zero", YT_PORT="0")
        self._refused("negative", YT_PORT="-1")
        self._refused("over 16 bits", YT_PORT="65536")
        self._refused("far over", YT_PORT="99999")

    def test_out_of_range_threads(self):
        self._refused("negative", YT_WHISPER_THREADS="-1")
        self._refused("absurd", YT_WHISPER_THREADS="1000")

    def test_bad_languages(self):
        self._refused("whitespace only", YT_LANGUAGE="  ")
        self._refused("a path", YT_LANGUAGE="../../etc/passwd")
        self._refused("digits", YT_LANGUAGE="en1")
        self._refused("a space", YT_LANGUAGE="en us")
        self._refused("punctuation", YT_LANGUAGE="en;rm -rf /")

    def test_a_language_that_looks_like_a_flag_is_refused(self):
        """Found by this test, which is why it has its own case.

        `YT_LANGUAGE` becomes the operand of `-l` in whisper-cli's argv. A value of
        `-m` would be parsed as the next *flag* instead of as this one's argument,
        which turns a language setting into a way to pick the model file. Hyphens
        are legal inside a code (`zh-tw`), so the check is on the edges.
        """
        self._refused("a bare flag", YT_LANGUAGE="-m")
        self._refused("a long flag", YT_LANGUAGE="--model")
        self._refused("a trailing hyphen", YT_LANGUAGE="en-")

    def test_the_error_names_the_variable(self):
        """An operator reading a refusal should not have to guess which one."""
        with self.assertRaises(config.ConfigError) as caught:
            load(YT_MAX_DURATION_S="abc")
        self.assertIn("YT_MAX_DURATION_S", str(caught.exception))


class TestDnsServers(unittest.TestCase):
    """`YT_DNS_SERVERS` is written into /etc/resolv.conf by root."""

    def test_unset_is_empty(self):
        self.assertEqual(load().dns_servers, ())
        self.assertIsNone(config.dns_servers({}))
        self.assertIsNone(config.dns_servers({"YT_DNS_SERVERS": " "}))

    def test_spaces_and_commas_both_separate(self):
        self.assertEqual(
            load(YT_DNS_SERVERS="9.9.9.9, 1.1.1.1 2620:fe::fe").dns_servers,
            ("9.9.9.9", "1.1.1.1", "2620:fe::fe"),
        )

    def test_addresses_are_normalised(self):
        self.assertEqual(
            config.dns_servers({"YT_DNS_SERVERS": "2620:00fe:0::fe"}),
            ("2620:fe::fe",),
        )

    def test_refused(self):
        for because, value in (
            ("a hostname", "dns.google"),
            ("a newline smuggling an option", "1.1.1.1\noptions ndots:15"),
            ("a port", "1.1.1.1:53"),
            ("leading zeros", "010.0.0.1"),
            ("more than glibc reads", "1.1.1.1 1.0.0.1 9.9.9.9 8.8.8.8"),
        ):
            with self.subTest(because=because):
                with self.assertRaises(config.ConfigError):
                    load(YT_DNS_SERVERS=value)


class TestCpuCount(unittest.TestCase):
    def test_it_is_at_least_one(self):
        self.assertGreaterEqual(config.available_cpus(), 1)

    def test_it_is_the_default_thread_count(self):
        self.assertEqual(
            config.load(env={}).threads, config.available_cpus()
        )


class TestBoundaries(unittest.TestCase):
    def test_inclusive_edges_are_accepted(self):
        self.assertEqual(load(YT_PORT="1").port, 1)
        self.assertEqual(load(YT_PORT="65535").port, 65535)
        self.assertEqual(load(YT_MAX_DURATION_S="1").max_duration_s, 1)
        self.assertEqual(load(YT_MAX_DURATION_S="86400").max_duration_s, 86400)


if __name__ == "__main__":
    unittest.main()
