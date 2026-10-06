"""The security boundary, tested the way a boundary should be: mostly negatives.

`urls.validate` is what decides whether this service fetches a URL at all. Every
other control narrows what a fetch does; this one decides that it happens. So the
cases below are weighted towards the strings that *look* like YouTube URLs and are
not, because those are the ones a substring check would have let through.

stdlib `unittest`, no network, no model, no container.
"""

import unittest

import urls


class TestAccepted(unittest.TestCase):
    """The five hosts the service declares, in the shapes they really occur."""

    def test_the_declared_hosts(self):
        for url in (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/watch?v=dQw4w9WgXcQ",
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
        ):
            with self.subTest(url=url):
                self.assertEqual(urls.validate(url), url)

    def test_http_is_accepted_as_well_as_https(self):
        url = "http://www.youtube.com/watch?v=dQw4w9WgXcQ"
        self.assertEqual(urls.validate(url), url)

    def test_the_original_string_is_returned_not_a_normalisation(self):
        """What is fetched must be what was checked.

        If validate returned a reconstructed URL, the string that passed the host
        check and the string handed to yt-dlp would be two different objects, and
        every argument about the host check would be about the wrong one.
        """
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s"
        self.assertIs(urls.validate(url), url)

    def test_surrounding_whitespace_is_tolerated(self):
        self.assertEqual(
            urls.validate("  https://youtu.be/dQw4w9WgXcQ  "),
            "https://youtu.be/dQw4w9WgXcQ",
        )

    def test_host_case_does_not_matter(self):
        url = "https://WWW.YouTube.COM/watch?v=dQw4w9WgXcQ"
        self.assertEqual(urls.validate(url), url)

    def test_trailing_dot_is_the_same_host(self):
        """`youtube.com.` is the DNS root form and resolves identically."""
        url = "https://www.youtube.com./watch?v=dQw4w9WgXcQ"
        self.assertEqual(urls.validate(url), url)


class TestRefusedHosts(unittest.TestCase):
    """The near-misses. Each of these is one way to get a host check wrong."""

    def _refused(self, url, because):
        with self.subTest(url=url, because=because):
            with self.assertRaises(urls.UrlError):
                urls.validate(url)

    def test_suffix_attack(self):
        self._refused(
            "https://youtube.com.attacker.example/watch?v=dQw4w9WgXcQ",
            "an endswith() or substring check says yes to this",
        )

    def test_prefix_attack(self):
        self._refused(
            "https://attacker.example/www.youtube.com/watch?v=x",
            "the allowed host appears in the path",
        )

    def test_userinfo_attack(self):
        self._refused(
            "https://www.youtube.com@attacker.example/watch?v=dQw4w9WgXcQ",
            "netloc starts with the allowed host; hostname is the attacker's",
        )

    def test_query_string_mention(self):
        self._refused(
            "https://attacker.example/?u=https://www.youtube.com/watch?v=x",
            "the whole URL contains the allowed host",
        )

    def test_fragment_mention(self):
        self._refused(
            "https://attacker.example/#www.youtube.com",
            "so does this",
        )

    def test_subdomain_not_on_the_list(self):
        self._refused(
            "https://evil.youtube.com/watch?v=dQw4w9WgXcQ",
            "the list is exact hosts, not a domain suffix",
        )

    def test_nocookie_domain_is_not_on_the_list(self):
        self._refused(
            "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
            "deliberately absent: same videos, reachable by the ordinary form",
        )

    def test_unicode_lookalike_host(self):
        self._refused(
            "https://www.yout\u0173be.com/watch?v=dQw4w9WgXcQ",
            "a homograph is a different host and must compare unequal",
        )

    def test_a_bare_ip(self):
        self._refused("https://142.251.150.4/watch?v=x", "not a name on the list")


class TestRefusedShapes(unittest.TestCase):
    """Things that are not an acceptable URL at all."""

    def _refused(self, value, because):
        with self.subTest(value=repr(value)[:60], because=because):
            with self.assertRaises(urls.UrlError):
                urls.validate(value)

    def test_non_string_types(self):
        for value in (None, 42, [], {}, b"https://www.youtube.com/watch?v=x"):
            self._refused(value, "only a string is a URL")

    def test_empty_and_whitespace(self):
        self._refused("", "empty")
        self._refused("   ", "whitespace only")

    def test_file_scheme(self):
        self._refused(
            "file:///etc/passwd",
            "yt-dlp reads file: URLs; this is why the scheme is checked",
        )

    def test_other_schemes(self):
        for value in (
            "ftp://www.youtube.com/x",
            "data:text/plain;base64,aGk=",
            "javascript:alert(1)",
            "gopher://www.youtube.com/",
        ):
            self._refused(value, "not http(s)")

    def test_scheme_relative_and_bare(self):
        self._refused("//www.youtube.com/watch?v=x", "no scheme")
        self._refused("www.youtube.com/watch?v=x", "no scheme")

    def test_control_characters(self):
        """Checked before parsing, because urlsplit silently strips some of them."""
        self._refused(
            "https://www.youtube.com/watch?v=x\nX-Injected: 1",
            "a newline would smuggle a second line into anything that logs it",
        )
        self._refused("https://www.youtube.com/\rwatch?v=x", "carriage return")
        self._refused("https://www.youtube.com/\twatch?v=x", "tab")
        self._refused("https://www.youtube.com/watch?v=x\x00", "NUL")

    def test_absurd_length(self):
        self._refused(
            "https://www.youtube.com/watch?v=" + "a" * 4000,
            "a megabyte of query string is not a video id",
        )

    def test_length_boundary_is_inclusive(self):
        base = "https://www.youtube.com/watch?v="
        exactly = base + "a" * (2048 - len(base))
        self.assertEqual(len(exactly), 2048)
        self.assertEqual(urls.validate(exactly), exactly)

    def test_shell_metacharacters_are_not_special(self):
        """They are refused for their *host*, not for their punctuation.

        Nothing in this service ever builds a shell command, so `;` is not dangerous
        here -- and a URL on an allowed host that happens to contain one is fetched
        normally. This test pins both halves so a future reader does not add shell
        escaping and conclude it was load-bearing.
        """
        with self.assertRaises(urls.UrlError):
            urls.validate("https://evil.example/x; rm -rf /")
        allowed = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=a;b"
        self.assertEqual(urls.validate(allowed), allowed)


class TestCustomAllowList(unittest.TestCase):
    def test_the_allow_list_is_injectable(self):
        self.assertEqual(
            urls.validate("https://example.test/x", allowed={"example.test"}),
            "https://example.test/x",
        )

    def test_an_empty_allow_list_refuses_everything(self):
        with self.assertRaises(urls.UrlError):
            urls.validate("https://www.youtube.com/watch?v=x", allowed=set())


class TestVideoId(unittest.TestCase):
    """Reported, never used to build a request. None is an ordinary answer."""

    def test_the_url_shapes_that_carry_one(self):
        for url in (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30s",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ?t=30",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "https://www.youtube.com/live/dQw4w9WgXcQ",
            "https://www.youtube.com/embed/dQw4w9WgXcQ",
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
        ):
            with self.subTest(url=url):
                self.assertEqual(urls.video_id(url), "dQw4w9WgXcQ")

    def test_urls_that_carry_none(self):
        for url in (
            "https://www.youtube.com/",
            "https://www.youtube.com/watch",
            "https://www.youtube.com/playlist?list=PLxyz",
            "https://www.youtube.com/@someone",
        ):
            with self.subTest(url=url):
                self.assertIsNone(urls.video_id(url))

    def test_wrong_length_is_not_an_id(self):
        self.assertIsNone(urls.video_id("https://www.youtube.com/watch?v=short"))
        self.assertIsNone(
            urls.video_id("https://www.youtube.com/watch?v=" + "a" * 40)
        )

    def test_characters_outside_the_alphabet_are_not_an_id(self):
        """What this returns goes into a JSON response, so its shape is checked."""
        self.assertIsNone(urls.video_id("https://www.youtube.com/watch?v=abc/def<hi"))

    def test_it_never_raises_on_a_hostile_string(self):
        for url in ("", "://", "https://", "not a url", "file:///etc/passwd"):
            with self.subTest(url=url):
                self.assertIsNone(urls.video_id(url))


if __name__ == "__main__":
    unittest.main()
