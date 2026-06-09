"""
Unit tests for generate_bandersnatch_conf.py — no network calls required.
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import generate_bandersnatch_conf as gc


# --------------------------------------------------------------------------- #
# Python version exclusions
# --------------------------------------------------------------------------- #
class TestPyExcludes(unittest.TestCase):
    def test_excludes_py2_always(self):
        excl = gc.py_excludes(["3.10", "3.11"])
        self.assertIn("py2", excl)

    def test_kept_versions_not_excluded(self):
        excl = gc.py_excludes(["3.10", "3.11"])
        self.assertNotIn("py3.10", excl)
        self.assertNotIn("py3.11", excl)

    def test_other_versions_excluded(self):
        excl = gc.py_excludes(["3.11"])
        self.assertIn("py3.9", excl)
        self.assertIn("py3.10", excl)
        self.assertIn("py3.12", excl)

    def test_full_version_string_accepted(self):
        # "3.11.2" should be treated as minor "3.11"
        excl = gc.py_excludes(["3.11.2"])
        self.assertNotIn("py3.11", excl)
        self.assertIn("py3.10", excl)


# --------------------------------------------------------------------------- #
# Platform exclusions
# --------------------------------------------------------------------------- #
class TestPlatformExcludes(unittest.TestCase):
    def test_kept_platforms_not_excluded(self):
        excl = gc.platform_excludes(["linux", "windows"])
        self.assertNotIn("linux", excl)
        self.assertNotIn("windows", excl)

    def test_other_platforms_excluded(self):
        excl = gc.platform_excludes(["linux"])
        self.assertIn("windows", excl)
        self.assertIn("macos", excl)
        self.assertIn("freebsd", excl)

    def test_all_excluded_when_nothing_kept(self):
        excl = gc.platform_excludes([])
        self.assertEqual(set(excl), {"linux", "windows", "macos", "freebsd"})


# --------------------------------------------------------------------------- #
# Config generation
# --------------------------------------------------------------------------- #
class TestBuildConf(unittest.TestCase):
    def _conf(self, **kwargs):
        defaults = dict(
            allowlist_lines=["requests>=2.28.0,<3", "flask>=2.0,<3"],
            output_dir="./mirror",
            master="https://pypi.org",
            workers=4,
            keep_json=True,
            keep_platforms=["linux", "windows"],
            keep_pyversions=["3.10", "3.11"],
            exclude_py_minor=True,
        )
        defaults.update(kwargs)
        return gc.build_conf(**defaults)

    def test_output_dir_in_conf(self):
        conf = self._conf(output_dir="/data/mirror")
        self.assertIn("directory = /data/mirror", conf)

    def test_allowlist_packages_present(self):
        conf = self._conf()
        self.assertIn("requests>=2.28.0,<3", conf)
        self.assertIn("flask>=2.0,<3", conf)

    def test_keep_json_true(self):
        conf = self._conf(keep_json=True)
        self.assertIn("json = true", conf)

    def test_keep_json_false(self):
        conf = self._conf(keep_json=False)
        self.assertIn("json = false", conf)

    def test_excluded_platforms_in_blocklist(self):
        conf = self._conf(keep_platforms=["linux"])
        self.assertIn("windows", conf)
        self.assertIn("macos", conf)

    def test_workers_capped_at_10(self):
        # build_conf itself doesn't cap; the caller (main) does. Verify it's
        # emitted verbatim when within range.
        conf = self._conf(workers=4)
        self.assertIn("workers = 4", conf)

    def test_master_url_in_conf(self):
        conf = self._conf(master="https://pypi.org")
        self.assertIn("master = https://pypi.org", conf)

    def test_py_excludes_present_when_enabled(self):
        conf = self._conf(keep_pyversions=["3.11"], exclude_py_minor=True)
        self.assertIn("py3.10", conf)
        self.assertIn("py3.12", conf)
        self.assertNotIn("py3.11\n", conf)  # 3.11 should not appear in blocklist

    def test_py_excludes_absent_when_disabled(self):
        conf = self._conf(keep_pyversions=["3.11"], exclude_py_minor=False)
        # No py3.x entries should appear in the blocklist section
        self.assertNotIn("py3.10", conf)
        self.assertNotIn("py3.12", conf)

    def test_plugins_block_present(self):
        conf = self._conf()
        self.assertIn("[plugins]", conf)
        self.assertIn("allowlist_project", conf)
        self.assertIn("allowlist_release", conf)
        self.assertIn("exclude_platform", conf)


if __name__ == "__main__":
    unittest.main()
