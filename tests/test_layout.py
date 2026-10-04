"""The pack tree, read the way `nodo pack` reads it.

A wrong `.service/` does not fail a test anywhere else. It fails a pack that can take
an hour, or worse, it packs and then never starts. These tests check, without a node,
the rules in nodo's `docs/PACKING.md` and the facts that tie `service.json` to the
code: the entrypoint exists and is executable, the declared envs are the envs the
code reads, the API port is the default port, and the Dockerfile follows the
packer's COPY rewrite.
"""

import json
import os
import re
import stat
import unittest

import config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICE_DIR = os.path.join(ROOT, ".service")


def _json(name):
    with open(os.path.join(SERVICE_DIR, name), encoding="utf-8") as handle:
        return json.load(handle)


def _dockerfile_instructions():
    """(instruction, rest) per Dockerfile instruction, continuations joined."""
    with open(os.path.join(SERVICE_DIR, "Dockerfile"), encoding="utf-8") as handle:
        text = handle.read()
    text = re.sub(r"\\\n", " ", text)
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        word, _, rest = stripped.partition(" ")
        yield word.upper(), rest.strip()


def _envs_read_by_the_code():
    names = set()
    for module in ("config.py", "resolver.py"):
        with open(os.path.join(ROOT, "service", module), encoding="utf-8") as handle:
            names |= set(re.findall(r'"(YT_[A-Z_]+)"', handle.read()))
    return names


class TestServiceJson(unittest.TestCase):
    def setUp(self):
        self.spec = _json("service.json")

    def test_the_architecture_is_declared(self):
        """The one field the packer refuses to go without."""
        self.assertIn(self.spec["architecture"], ("linux/arm64", "linux/amd64"))

    def test_the_entrypoint_is_in_the_image_and_executable(self):
        """`/init` refuses an entrypoint that is not executable, after boot."""
        entry = self.spec["init"]["entry_path"]
        self.assertEqual(entry, ["service", "entrypoint.sh"])
        path = os.path.join(ROOT, *entry)
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(os.stat(path).st_mode & stat.S_IXUSR)
        with open(path, encoding="utf-8") as handle:
            self.assertTrue(handle.readline().startswith("#!/bin/sh"))

    def test_the_legacy_entrypoint_field_is_not_used(self):
        self.assertNotIn("entrypoint", self.spec)

    def test_the_api_port_is_the_default_port(self):
        ports = [slot["port"] for slot in self.spec["api"]]
        self.assertEqual(ports, [config.load(env={}).port])

    def test_every_env_the_code_reads_is_declared_and_no_other(self):
        """nodo delivers declared envs only. An undeclared one never arrives."""
        self.assertEqual(set(self.spec["envs"]), _envs_read_by_the_code())

    def test_every_declared_env_is_delivered_as_a_real_env_var(self):
        """nodo exports a name to the process only if it has this shape."""
        for name in self.spec["envs"]:
            self.assertRegex(name, r"^[A-Za-z_][A-Za-z0-9_]*$")
            self.assertNotIn(name, ("LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT"))

    def test_every_network_has_tags_and_prose_and_no_glob(self):
        for network in self.spec["network"]:
            self.assertTrue(network["tags"])
            self.assertTrue(network["prose"].strip())
            for tag in network["tags"]:
                self.assertFalse(tag.startswith("*") and tag != "*", tag)

    def test_resources_give_whisper_two_cores(self):
        """nodo boots `ceil(cpu_quota / cpu_period)` vCPUs, and 1 with no quota."""
        at_init = self.spec["resources"]["at_init"]
        self.assertEqual(at_init["cpu_quota"] // at_init["cpu_period"], 2)

    def test_at_most_is_never_below_at_init(self):
        at_init = self.spec["resources"]["at_init"]
        at_most = self.spec["resources"]["at_most"]
        for key, value in at_init.items():
            self.assertGreaterEqual(at_most.get(key, value), value, key)


class TestPackConfig(unittest.TestCase):
    def test_the_included_paths_exist(self):
        for item in _json("pack_config.json")["include"]:
            self.assertTrue(os.path.exists(os.path.join(ROOT, item)), item)

    def test_tests_are_not_packed(self):
        self.assertNotIn("tests", _json("pack_config.json")["include"])


class TestDockerfile(unittest.TestCase):
    def setUp(self):
        self.instructions = list(_dockerfile_instructions())

    def test_no_runtime_metadata(self):
        """The packer exports a filesystem. These would be ignored or misleading."""
        for word, _rest in self.instructions:
            self.assertNotIn(word, ("CMD", "ENTRYPOINT", "EXPOSE", "ENV", "USER"))

    def test_every_context_copy_starts_with_dot_slash(self):
        """The packer rewrites `./x` to `service/x`. A bare `x` is not rewritten."""
        for word, rest in self.instructions:
            if word != "COPY" or "--from=" in rest:
                continue
            sources = [p for p in rest.split() if not p.startswith("--")][:-1]
            for source in sources:
                self.assertTrue(source.startswith("./"), rest)

    def test_the_service_code_lands_where_entry_path_points(self):
        copies = [rest for word, rest in self.instructions if word == "COPY"]
        self.assertIn("./service /service", copies)

    def test_every_service_module_is_imported_by_the_smoke_test(self):
        """A module the build does not import is a module the build does not check."""
        runs = " ".join(rest for word, rest in self.instructions if word == "RUN")
        match = re.search(r'python3 -c "import ([^"]+)"', runs)
        self.assertIsNotNone(match)
        imported = {name.strip() for name in match.group(1).split(",")}
        modules = {
            name[:-3] for name in os.listdir(os.path.join(ROOT, "service"))
            if name.endswith(".py")
        }
        self.assertEqual(imported, modules)


if __name__ == "__main__":
    unittest.main()
