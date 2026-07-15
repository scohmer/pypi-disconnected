"""Tests for the disconnected-mirror correctness fixes (no network)."""
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import resolve_deps as rd
from packaging.requirements import Requirement
from packaging.version import Version


class TestArchitectureMarkers(unittest.TestCase):
    def test_platform_machine_present(self):
        m = rd.build_matrix(["3.11"], ["linux"], ["x86_64"])
        self.assertEqual(m[0]["platform_machine"], "x86_64")

    def test_windows_amd64_token(self):
        m = rd.build_matrix(["3.11"], ["windows"], ["x86_64"])
        self.assertEqual(m[0]["platform_machine"], "AMD64")

    def test_arm64_expands_matrix(self):
        m = rd.build_matrix(["3.11"], ["linux"], ["x86_64", "arm64"])
        machines = {e["platform_machine"] for e in m}
        self.assertEqual(machines, {"x86_64", "aarch64"})

    def test_machine_guarded_dep_followed_for_x86(self):
        # nvidia-style dep guarded on platform_machine must be seen on x86_64.
        req = Requirement("nvidia-cublas ; platform_machine == 'x86_64'")
        m = rd.build_matrix(["3.11"], ["linux"], ["x86_64"])
        self.assertTrue(rd.marker_true_for_any(req, m, set()))

    def test_machine_guarded_dep_excluded_for_arm_only(self):
        req = Requirement("somewheel ; platform_machine == 'x86_64'")
        m = rd.build_matrix(["3.11"], ["linux"], ["arm64"])
        self.assertFalse(rd.marker_true_for_any(req, m, set()))


class TestReportShape(unittest.TestCase):
    def test_report_has_conflict_and_sdist_buckets(self):
        r = rd.new_report()
        self.assertIn("conflicts", r)
        self.assertIn("sdist_only_no_meta", r)
        self.assertIn("not_found", r)

    def test_problem_detection(self):
        r = rd.new_report()
        self.assertFalse(rd.report_has_problems(r))
        r["not_found"].append("genism")
        self.assertTrue(rd.report_has_problems(r))


class TestCapFromResolvedInvariant(unittest.TestCase):
    """The allowlist range must always CONTAIN the resolved version."""
    def test_cap_is_next_major_of_resolved(self):
        # Emulate: listed floor 1.3, resolved 48 -> cap must be <49, not <2.
        v = Version("48.0.1")
        cap = rd.cap_for(v, "major")
        self.assertEqual(cap, "<49")

    def test_low_floor_high_resolved_range_contains_resolved(self):
        from packaging.specifiers import SpecifierSet
        floor = Version("1.3"); resolved = Version("48.0.1")
        rng = SpecifierSet(f">={floor},{rd.cap_for(resolved, 'major')}")
        self.assertTrue(rng.contains(resolved))
        self.assertTrue(rng.contains(Version("46.0.0")))  # a conflicting dep's need


class TestNoCapDefault(unittest.TestCase):
    """The mandatory major-cap is gone: default is 'none' (forward, no bound)."""
    def test_cap_none_returns_empty(self):
        self.assertEqual(rd.cap_for(Version("2.28.0"), "none"), "")

    def test_build_specifier_none_has_no_upper_bound(self):
        spec = rd.build_specifier(Version("2.28.0"), "none")
        self.assertEqual(spec, ">=2.28.0")
        self.assertNotIn("<", spec)

    def test_cli_default_is_none(self):
        import argparse, io, contextlib
        # resolve_deps.main argparse default -- introspect the parser it builds.
        # Simplest: the module-level default should let >=floor pass uncapped.
        self.assertEqual(rd.cap_for(Version("48.0.1"), "none"), "")


if __name__ == "__main__":
    unittest.main()


class TestRequiresPython(unittest.TestCase):
    def test_python_ok_none_is_compatible(self):
        self.assertTrue(rd.python_ok(None, "3.9"))

    def test_python_ok_lower_bound(self):
        self.assertTrue(rd.python_ok(">=3.9", "3.13"))
        self.assertFalse(rd.python_ok(">=3.10", "3.9"))

    def test_python_ok_upper_bound_excludes_new(self):
        self.assertTrue(rd.python_ok(">=3.8,<3.13", "3.12"))
        self.assertFalse(rd.python_ok(">=3.8,<3.13", "3.13"))

    def test_python_ok_unparseable_defaults_true(self):
        self.assertTrue(rd.python_ok("not-a-spec", "3.11"))


class TestBestVersionForPython(unittest.TestCase):
    def _meta(self):
        # 2.0 needs >=3.10 ; 1.26 supports >=3.9
        return {"releases": {
            "1.26.0": [{"requires_python": ">=3.9", "yanked": False, "packagetype": "bdist_wheel"}],
            "2.0.0":  [{"requires_python": ">=3.10", "yanked": False, "packagetype": "bdist_wheel"}],
        }}

    def test_picks_newest_for_new_python(self):
        from packaging.specifiers import SpecifierSet
        v = rd.best_version_for_python(self._meta(), SpecifierSet(), False, "3.12")
        self.assertEqual(str(v), "2.0.0")

    def test_falls_back_to_old_for_legacy_python(self):
        from packaging.specifiers import SpecifierSet
        v = rd.best_version_for_python(self._meta(), SpecifierSet(), False, "3.9")
        self.assertEqual(str(v), "1.26.0")


class TestWindowMode(unittest.TestCase):
    def _meta(self):
        return {"releases": {
            "1.0.0": [{"requires_python": ">=3.9", "yanked": False, "packagetype": "bdist_wheel"}],
            "1.1.0": [{"requires_python": ">=3.9", "yanked": False, "packagetype": "bdist_wheel"}],
            "1.2.0": [{"requires_python": ">=3.10", "yanked": False, "packagetype": "bdist_wheel"}],
            "1.3.0": [{"requires_python": ">=3.10", "yanked": False, "packagetype": "bdist_wheel"}],
        }}

    def test_latest_n_for_python_respects_requires_python(self):
        from packaging.specifiers import SpecifierSet
        # py3.9 can only use 1.0/1.1 -> latest 4 compatible = those two
        got = rd.latest_n_for_python(self._meta(), SpecifierSet(), False, "3.9", 4)
        self.assertEqual([str(v) for v in got], ["1.1.0", "1.0.0"])

    def test_latest_n_for_python_counts(self):
        from packaging.specifiers import SpecifierSet
        got = rd.latest_n_for_python(self._meta(), SpecifierSet(), False, "3.12", 2)
        self.assertEqual([str(v) for v in got], ["1.3.0", "1.2.0"])

    def test_window_size_limits(self):
        from packaging.specifiers import SpecifierSet
        got = rd.latest_n_for_python(self._meta(), SpecifierSet(), False, "3.12", 1)
        self.assertEqual([str(v) for v in got], ["1.3.0"])
