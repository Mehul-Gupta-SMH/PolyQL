"""Agent-harness packaging: Claude Code plugin + marketplace, Codex manifests, launcher."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "polyql"
LAUNCHER = PLUGIN / "bin" / "polyql-mcp"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _frontmatter(path: Path) -> dict:
    m = re.match(r"---\n(.*?)\n---\n", path.read_text(encoding="utf-8"), re.S)
    assert m, f"{path} has no frontmatter"
    return dict(
        line.split(":", 1) for line in m.group(1).splitlines() if ":" in line and not line.startswith(" ")
    )


def test_marketplace_points_at_plugin_dir():
    mp = _load(ROOT / ".claude-plugin" / "marketplace.json")
    assert mp["name"] and mp["owner"]["name"]
    entry = next(p for p in mp["plugins"] if p["name"] == "polyql")
    assert entry["source"] == "./plugins/polyql" and ".." not in entry["source"]
    assert (ROOT / entry["source"] / ".claude-plugin" / "plugin.json").is_file()


def test_plugin_manifest_name_matches_marketplace_entry():
    manifest = _load(PLUGIN / ".claude-plugin" / "plugin.json")
    assert manifest["name"] == "polyql"
    assert manifest["version"] == _load(PLUGIN / ".codex-plugin" / "plugin.json")["version"]


def test_mcp_server_uses_bundled_launcher():
    server = _load(PLUGIN / ".claude-plugin" / "plugin.json")["mcpServers"]["polyql"]
    assert server["command"] == "${CLAUDE_PLUGIN_ROOT}/bin/polyql-mcp"
    assert LAUNCHER.is_file() and os.access(LAUNCHER, os.X_OK)


def test_user_config_references_are_declared():
    manifest = _load(PLUGIN / ".claude-plugin" / "plugin.json")
    declared = set(manifest["userConfig"])
    blob = json.dumps(manifest["mcpServers"])
    for ref in re.findall(r"\$\{user_config\.(\w+)\}", blob):
        assert ref in declared
    for cmd in (PLUGIN / "commands").glob("*.md"):
        for ref in re.findall(r"\$\{user_config\.(\w+)\}", cmd.read_text(encoding="utf-8")):
            assert ref in declared, f"{cmd.name}: {ref}"
    for key, opt in manifest["userConfig"].items():
        assert {"type", "title", "description"} <= set(opt), key


def test_skill_exists_with_frontmatter():
    skill = PLUGIN / "skills" / "polyql-sql" / "SKILL.md"
    fm = _frontmatter(skill)
    assert fm["name"].strip() == "polyql-sql" and fm["description"].strip()
    assert not (ROOT / "docs" / "agent-skill").exists(), "skill must have a single source"


@pytest.mark.parametrize("name", ["sql", "schema", "explain-join"])
def test_commands_exist(name):
    cmd = PLUGIN / "commands" / f"{name}.md"
    assert _frontmatter(cmd)["description"].strip()
    assert "$ARGUMENTS" in cmd.read_text(encoding="utf-8")


def test_codex_manifests():
    manifest = _load(PLUGIN / ".codex-plugin" / "plugin.json")
    assert manifest["name"] == "polyql"
    assert (PLUGIN / manifest["skills"] / "polyql-sql" / "SKILL.md").is_file()
    mp = _load(ROOT / ".agents" / "plugins" / "marketplace.json")
    entry = mp["plugins"][0]
    assert entry["source"]["path"].startswith("./")
    assert (ROOT / entry["source"]["path"] / ".codex-plugin" / "plugin.json").is_file()


def test_codex_mcp_server_runs_launcher_from_plugin_root():
    # Codex resolves a relative command against the session cwd, so the server goes
    # through `sh -c` with cwd = plugin root; keys are Codex's snake_case config fields.
    server = _load(PLUGIN / ".codex-plugin" / "plugin.json")["mcpServers"]["polyql"]
    assert set(server) <= {"command", "args", "cwd", "env_vars", "startup_timeout_sec"}
    assert server["command"] == "sh" and server["cwd"] == "."
    assert "POLYQL_HOME" in server["env_vars"]
    script = server["args"][-1]
    r = subprocess.run(["sh", "-c", f"{script} --help"], cwd=PLUGIN, capture_output=True,
                       text=True, timeout=60, env={**os.environ, "POLYQL_HOME": str(ROOT)})
    assert r.returncode == 0 and "--transport" in r.stdout


def test_console_script_target_is_callable():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'polyql-mcp = "backend.mcp_server:main"' in pyproject
    import ast

    tree = ast.parse((ROOT / "backend" / "mcp_server.py").read_text(encoding="utf-8"))
    assert any(isinstance(n, ast.FunctionDef) and n.name == "main" for n in tree.body)


def _run(env_extra: dict, *args: str, cwd=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("POLYQL_")}
    env.update(env_extra)
    return subprocess.run(
        [str(LAUNCHER), *args], capture_output=True, text=True, env=env, cwd=cwd, timeout=60
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell launcher")
def test_launcher_rejects_bad_home(tmp_path):
    r = _run({"POLYQL_HOME": str(tmp_path)}, "--help")
    assert r.returncode == 1 and "POLYQL_HOME" in r.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell launcher")
def test_launcher_not_found_outside_clone(tmp_path):
    copy = tmp_path / "plugin"
    import shutil

    shutil.copytree(PLUGIN, copy)
    env = {k: v for k, v in os.environ.items() if not k.startswith("POLYQL_")}
    r = subprocess.run([str(copy / "bin" / "polyql-mcp"), "--help"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 1 and "set POLYQL_HOME" in r.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell launcher")
def test_launcher_runs_server_help(tmp_path):
    # Empty values (unset plugin options) must be treated as unset.
    r = _run({"POLYQL_PYTHON": sys.executable, "POLYQL_HOME": "", "POLYQL_MCP_DB": ""},
             "--help", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "--enable-generate" in r.stdout
