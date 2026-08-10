# tests/test_impact.py
from clio.impact import impact_of_module, impact_of_symbol
from clio.reports import ReportArchive


def test_symbol_direct_callers(tmp_path, seed_job):
    root = tmp_path / "root"
    seed_job(root, "job-x", "2026-08-10T00:00:00+00:00",
             {"one.py": "def f():\n    return 1\n\ndef g():\n    return f()\n"})
    archive = ReportArchive(root)
    impact = impact_of_symbol(archive, "job-x", "one::f")
    assert impact.callers == [("one::g", 5)]
    assert impact.affected_modules == ["one"]
    assert impact.clusters_hit == ["one"]
    assert impact.verdict == "contained"


def test_symbol_transitive_callers(tmp_path, seed_job):
    root = tmp_path / "root"
    seed_job(root, "job-x", "2026-08-10T00:00:00+00:00",
             {"chain.py": "def a():\n    return 1\n\ndef b():\n    return a()\n\ndef c():\n    return b()\n"})
    impact = impact_of_symbol(ReportArchive(root), "job-x", "chain::a", depth=2)
    assert impact.callers == [("chain::b", 5), ("chain::c", 8)]


def test_symbol_depth_cap(tmp_path, seed_job):
    root = tmp_path / "root"
    seed_job(root, "job-x", "2026-08-10T00:00:00+00:00",
             {"chain.py": "def a():\n    return 1\n\ndef b():\n    return a()\n\ndef c():\n    return b()\n"})
    impact = impact_of_symbol(ReportArchive(root), "job-x", "chain::a", depth=1)
    assert impact.callers == [("chain::b", 5)]


def test_symbol_cross_cutting(tmp_path, seed_job):
    root = tmp_path / "root"
    seed_job(root, "job-x", "2026-08-10T00:00:00+00:00", {
        "pkg_a/__init__.py": "",
        "pkg_a/one.py": "def f():\n    return 1\n\ndef g():\n    return f()\n",
        "pkg_b/__init__.py": "",
        "pkg_b/two.py": "import pkg_a.one\n",
    })
    impact = impact_of_symbol(ReportArchive(root), "job-x", "pkg_a.one::f")
    assert impact.callers == [("pkg_a.one::g", 5)]
    assert impact.affected_modules == ["pkg_a.one", "pkg_b.two"]
    assert impact.clusters_hit == ["pkg_a", "pkg_b"]
    assert impact.verdict == "cross-cutting"


def test_symbol_missing(tmp_path, seed_job):
    root = tmp_path / "root"
    seed_job(root, "job-x", "2026-08-10T00:00:00+00:00", {"one.py": "def f():\n    return 1\n"})
    impact = impact_of_symbol(ReportArchive(root), "job-x", "nope::missing")
    assert impact.verdict == "missing"
    assert impact.affected_modules == [] and impact.callers == []


def test_module_contained(tmp_path, seed_job):
    root = tmp_path / "root"
    seed_job(root, "job-x", "2026-08-10T00:00:00+00:00", {"one.py": "def f():\n    return 1\n"})
    impact = impact_of_module(ReportArchive(root), "job-x", "one")
    assert impact.affected_modules == ["one"]
    assert impact.verdict == "contained"


def test_module_transitive_importers(tmp_path, seed_job):
    root = tmp_path / "root"
    seed_job(root, "job-x", "2026-08-10T00:00:00+00:00", {
        "pkg_a/__init__.py": "",
        "pkg_a/one.py": "def f():\n    return 1\n",
        "pkg_b/__init__.py": "",
        "pkg_b/two.py": "import pkg_a.one\n",
        "pkg_c/__init__.py": "",
        "pkg_c/three.py": "import pkg_b.two\n",
    })
    impact = impact_of_module(ReportArchive(root), "job-x", "pkg_a.one", depth=2)
    assert impact.affected_modules == ["pkg_a.one", "pkg_b.two", "pkg_c.three"]
    assert impact.verdict == "cross-cutting"


def test_symbol_impact_src_layout(tmp_path, seed_job):
    root = tmp_path / "root"
    seed_job(root, "job-x", "2026-08-10T00:00:00+00:00", {
        "src/clio/__init__.py": "",
        "src/clio/core.py": "def f():\n    return 1\n",
        "src/clio/main.py": "from clio.core import f\n",
    })
    impact = impact_of_symbol(ReportArchive(root), "job-x", "src.clio.core::f")
    assert impact.affected_modules == ["src.clio.main"]
    assert impact.verdict == "contained"
