"""Tests for scripts/check_ci_requirements.py (CI7)."""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "check_ci_requirements",
    Path(__file__).resolve().parent.parent / "scripts" / "check_ci_requirements.py",
)
cc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cc)


def _write(tmp_path, reqs, pyproject):
    r = tmp_path / "requirements-ci.txt"
    p = tmp_path / "pyproject.toml"
    r.write_text(reqs)
    p.write_text(pyproject)
    return r, p


def test_normalise():
    assert cc.normalise("Flag_Embedding.X") == "flag-embedding-x"
    assert cc.normalise("PyYAML") == "pyyaml"


def test_requirement_name_ignores_noise():
    f = cc.requirement_name
    assert f("fastapi>=0.110") == "fastapi"
    assert f("uvicorn[standard]>=0.29") == "uvicorn"
    assert f("mcp>=2.2,<3        # note") == "mcp"
    assert f('foo; python_version<"3.11"') == "foo"
    assert f("python_dotenv==1.0") == "python-dotenv"
    assert f("# comment") is None
    assert f("") is None
    assert f("-r other.txt") is None


def test_parse_pyproject_runtime_and_optional(tmp_path):
    _, p = _write(tmp_path, "", '[project]\nname="x"\ndependencies=["a>=1","B_c"]\n'
                  '[project.optional-dependencies]\ndev=["pytest>=7"]\n')
    runtime, declared = cc.parse_pyproject(p)
    assert runtime == {"a", "b-c"}
    assert declared == {"a", "b-c", "pytest"}


def test_main_ok(tmp_path, capsys):
    r, p = _write(tmp_path, "a>=1\npytest\nruff\n",
                  '[project]\nname="x"\ndependencies=["a"]\n'
                  '[project.optional-dependencies]\ndev=["pytest"]\n')
    assert cc.main(["--requirements", str(r), "--pyproject", str(p)]) == 0
    assert "OK" in capsys.readouterr().out


def test_main_ci_package_undeclared(tmp_path, capsys):
    r, p = _write(tmp_path, "a\nPyJWT>=2\n", '[project]\nname="x"\ndependencies=["a"]\n')
    assert cc.main(["--requirements", str(r), "--pyproject", str(p)]) == 1
    assert "'pyjwt' is in requirements-ci.txt" in capsys.readouterr().out


def test_main_runtime_dep_missing_from_ci(tmp_path, capsys):
    r, p = _write(tmp_path, "a\n", '[project]\nname="x"\ndependencies=["a","newdep>=1"]\n')
    assert cc.main(["--requirements", str(r), "--pyproject", str(p)]) == 1
    assert "'newdep' is a runtime dependency" in capsys.readouterr().out


def test_allow_list_suppresses_heavy_packages():
    undeclared, missing = cc.check({"a"}, {"a", "torch", "chromadb"}, {"a", "torch", "chromadb"})
    assert undeclared == [] and missing == []


def test_name_variants_match():
    undeclared, missing = cc.check({"python-multipart"}, {"python-multipart"}, {"python-multipart"})
    assert (undeclared, missing) == ([], [])
    ci = cc.normalise("Python_Multipart")
    assert cc.check({ci}, {"python-multipart"}, {"python-multipart"}) == ([], [])


def test_repo_files_parse():
    # Real files must at least parse to non-empty sets.
    assert cc.parse_requirements(cc.ROOT / "requirements-ci.txt")
    runtime, declared = cc.parse_pyproject(cc.ROOT / "pyproject.toml")
    assert runtime and declared >= runtime
