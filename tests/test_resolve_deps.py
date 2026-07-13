"""
Unit tests for resolve_deps.py — no network calls required.
"""
import sys
import os
import types
import unittest

# Allow importing from scripts/ without an __init__.py
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import resolve_deps as rd


# --------------------------------------------------------------------------- #
# URL normalisation
# --------------------------------------------------------------------------- #
class TestToRawUrl(unittest.TestCase):
    def test_raw_url_passthrough(self):
        url = "https://raw.githubusercontent.com/owner/repo/main/requirements.txt"
        self.assertEqual(rd.to_raw_url(url, "requirements.txt"), url)

    def test_blob_url(self):
        url = "https://github.com/owner/repo/blob/main/requirements.txt"
        expected = "https://raw.githubusercontent.com/owner/repo/main/requirements.txt"
        self.assertEqual(rd.to_raw_url(url, "requirements.txt"), expected)

    def test_bare_repo_url_injects_template(self):
        url = "https://github.com/owner/repo"
        result = rd.to_raw_url(url, "requirements.txt")
        self.assertIn("{branch}", result)
        self.assertIn("requirements.txt", result)

    def test_trailing_slash_stripped(self):
        url = "https://github.com/owner/repo/"
        result = rd.to_raw_url(url, "requirements.txt")
        self.assertIn("{branch}", result)

    def test_non_github_url_passthrough(self):
        url = "https://example.com/requirements.txt"
        self.assertEqual(rd.to_raw_url(url, "requirements.txt"), url)


# --------------------------------------------------------------------------- #
# Requirements parsing
# --------------------------------------------------------------------------- #
class TestParseRequirements(unittest.TestCase):
    def test_simple(self):
        text = "requests==2.28.0\nflask>=2.0"
        reqs = rd.parse_requirements(text)
        names = [r.name for r in reqs]
        self.assertIn("requests", names)
        self.assertIn("flask", names)

    def test_blank_and_comment_lines_skipped(self):
        text = "\n# a comment\n  \nrequests==2.28.0\n"
        reqs = rd.parse_requirements(text)
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].name, "requests")

    def test_pip_flags_skipped_with_warning(self):
        import io
        text = "-r other.txt\nrequests==2.28.0"
        stderr = io.StringIO()
        old = sys.stderr
        sys.stderr = stderr
        try:
            reqs = rd.parse_requirements(text)
        finally:
            sys.stderr = old
        self.assertEqual(len(reqs), 1)
        self.assertIn("[warn]", stderr.getvalue())

    def test_marker_preserved(self):
        text = "requests>=2.0 ; python_version >= '3.8'"
        reqs = rd.parse_requirements(text)
        self.assertEqual(len(reqs), 1)
        self.assertIsNotNone(reqs[0].marker)

    def test_extras_parsed(self):
        text = "requests[security]==2.28.0"
        reqs = rd.parse_requirements(text)
        self.assertEqual(reqs[0].extras, {"security"})


# --------------------------------------------------------------------------- #
# Version bound helpers
# --------------------------------------------------------------------------- #
class TestLowerBound(unittest.TestCase):
    def _spec(self, s):
        from packaging.specifiers import SpecifierSet
        return SpecifierSet(s)

    def test_gte(self):
        v = rd.lower_bound(self._spec(">=2.28.0"))
        self.assertEqual(str(v), "2.28.0")

    def test_eq(self):
        v = rd.lower_bound(self._spec("==2.28.0"))
        self.assertEqual(str(v), "2.28.0")

    def test_compatible(self):
        v = rd.lower_bound(self._spec("~=2.28.0"))
        self.assertEqual(str(v), "2.28.0")

    def test_no_lower_bound_returns_none(self):
        v = rd.lower_bound(self._spec("<3.0"))
        self.assertIsNone(v)

    def test_multiple_picks_min(self):
        v = rd.lower_bound(self._spec(">=2.0,>=2.28.0"))
        self.assertEqual(str(v), "2.0")


class TestBuildSpecifier(unittest.TestCase):
    def _v(self, s):
        from packaging.version import Version
        return Version(s)

    def test_uncapped(self):
        self.assertEqual(rd.build_specifier(self._v("2.28.0")), ">=2.28.0")

    def test_zero_major_uncapped(self):
        self.assertEqual(rd.build_specifier(self._v("0.9.5")), ">=0.9.5")


# --------------------------------------------------------------------------- #
# Unversioned top-level requirements: latest + previous N releases
# --------------------------------------------------------------------------- #
class TestLatestNVersions(unittest.TestCase):
    def test_picks_newest_n_descending(self):
        meta = {"releases": {v: [{}] for v in ["1.0.0", "2.0.0", "3.0.0", "4.0.0", "5.0.0"]}}
        result = [str(v) for v in rd.latest_n_versions(meta, 4, include_pre=False)]
        self.assertEqual(result, ["5.0.0", "4.0.0", "3.0.0", "2.0.0"])

    def test_fewer_releases_than_n(self):
        meta = {"releases": {"1.0.0": [{}], "2.0.0": [{}]}}
        result = [str(v) for v in rd.latest_n_versions(meta, 4, include_pre=False)]
        self.assertEqual(result, ["2.0.0", "1.0.0"])

    def test_skips_yanked(self):
        meta = {"releases": {"1.0.0": [{"yanked": True}], "2.0.0": [{}]}}
        result = [str(v) for v in rd.latest_n_versions(meta, 4, include_pre=False)]
        self.assertEqual(result, ["2.0.0"])

    def test_excludes_prereleases_by_default(self):
        meta = {"releases": {"1.0.0": [{}], "2.0.0rc1": [{}]}}
        result = [str(v) for v in rd.latest_n_versions(meta, 4, include_pre=False)]
        self.assertEqual(result, ["1.0.0"])

    def test_includes_prereleases_when_requested(self):
        meta = {"releases": {"1.0.0": [{}], "2.0.0rc1": [{}]}}
        result = [str(v) for v in rd.latest_n_versions(meta, 4, include_pre=True)]
        self.assertEqual(result, ["2.0.0rc1", "1.0.0"])


# --------------------------------------------------------------------------- #
# Environment matrix
# --------------------------------------------------------------------------- #
class TestBuildMatrix(unittest.TestCase):
    def test_produces_one_env_per_py_platform_combo(self):
        matrix = rd.build_matrix(["3.10", "3.11"], ["linux", "windows"])
        self.assertEqual(len(matrix), 4)

    def test_env_has_expected_keys(self):
        matrix = rd.build_matrix(["3.10"], ["linux"])
        env = matrix[0]
        self.assertEqual(env["python_version"], "3.10")
        self.assertEqual(env["sys_platform"], "linux")
        self.assertEqual(env["implementation_name"], "cpython")

    def test_full_version_padded(self):
        matrix = rd.build_matrix(["3.10"], ["linux"])
        self.assertEqual(matrix[0]["python_full_version"], "3.10.0")

    def test_full_version_passthrough(self):
        matrix = rd.build_matrix(["3.10.5"], ["linux"])
        self.assertEqual(matrix[0]["python_full_version"], "3.10.5")


# --------------------------------------------------------------------------- #
# Marker evaluation
# --------------------------------------------------------------------------- #
class TestMarkerTrueForAny(unittest.TestCase):
    def _req(self, s):
        from packaging.requirements import Requirement
        return Requirement(s)

    def test_no_marker_always_true(self):
        req = self._req("requests>=2.0")
        matrix = rd.build_matrix(["3.10"], ["linux"])
        self.assertTrue(rd.marker_true_for_any(req, matrix, set()))

    def test_platform_marker_matches(self):
        req = self._req("winreg ; sys_platform == 'win32'")
        linux_matrix = rd.build_matrix(["3.10"], ["linux"])
        win_matrix = rd.build_matrix(["3.10"], ["windows"])
        self.assertFalse(rd.marker_true_for_any(req, linux_matrix, set()))
        self.assertTrue(rd.marker_true_for_any(req, win_matrix, set()))

    def test_python_version_marker(self):
        req = self._req("dataclasses ; python_version < '3.7'")
        matrix_38 = rd.build_matrix(["3.8"], ["linux"])
        matrix_36 = rd.build_matrix(["3.6"], ["linux"])
        self.assertFalse(rd.marker_true_for_any(req, matrix_38, set()))
        self.assertTrue(rd.marker_true_for_any(req, matrix_36, set()))

    def test_extra_marker(self):
        req = self._req("cryptography ; extra == 'security'")
        matrix = rd.build_matrix(["3.10"], ["linux"])
        self.assertFalse(rd.marker_true_for_any(req, matrix, set()))
        self.assertTrue(rd.marker_true_for_any(req, matrix, {"security"}))


if __name__ == "__main__":
    unittest.main()
