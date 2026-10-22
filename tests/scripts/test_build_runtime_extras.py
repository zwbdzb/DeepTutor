"""Extra-shipping gates in desktop-shell/tools/build_runtime.py.

The packaging script now ships the ``math-animator`` pyproject extra
(→ manim / scipy / av) into the offline desktop runtime. Four behaviours are
pinned here:

1. ``PIP_EXTRAS`` / ``EXTRA_SMOKE_MODULES`` stay consistent and reference a
   real extra declared in the repo's ``pyproject.toml``;
2. ``install_deeptutor()`` builds the pip target as
   ``<local-source>[extra1,extra2]`` on both the ``--no-deps`` fast path and
   the full-deps path, falling back to the bare source path when extras are
   empty (``subprocess.run`` is faked — nothing is ever installed);
3. ``dependencies_signature()`` folds the extras list into the hash so old
   ``.source-state.json`` files (bare pyproject hash) go stale and force one
   full reinstall on the next build;
4. ``smoke_test()`` fails the build when an extra lacks a module declaration
   or a declared module cannot be ``find_spec``-ed in the staging
   interpreter, and skips cleanly when ``PIP_EXTRAS`` is empty.

The module is loaded from its on-disk path (importlib) because
``build_runtime.py`` is a standalone tool, not an importable package. Loading
it is side-effect free: it only computes path constants and download URLs.
Nothing is fetched and no real staging tree is touched — ``build()`` runs
against a fake runtime tree with ``subprocess.run`` faked throughout, except
one end-to-end gate check against the live interpreter.

Windows note: no test decodes child-process bytes itself (the cp936 pitfall
from tests/desktop/test_toast_inject.py); the only real subprocess (e2e)
emits pure-ASCII output inside the code under test.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import py_compile
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_RUNTIME_PY = REPO_ROOT / "desktop-shell" / "tools" / "build_runtime.py"

MATH_ANIMATOR_MODULES = ("manim", "scipy", "av")


def _load_build_runtime_module():
    module_name = "build_runtime_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, BUILD_RUNTIME_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


br = _load_build_runtime_module()


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

PYPROJECT_STUB = (
    b"[project]\n"
    b'name = "stub-deeptutor"\n'
    b'version = "0.0.0"\n'
    b"\n"
    b"[project.optional-dependencies]\n"
    b'math-animator = ["manim>=0.19.0"]\n'
)


@pytest.fixture
def fake_source(tmp_path: Path) -> Path:
    """Minimal local source that passes install_deeptutor's guards."""
    source = tmp_path / "deeptutor-src"
    (source / "deeptutor_web").mkdir(parents=True)
    (source / "deeptutor_web" / "server.js").write_text("// stub\n", encoding="utf-8")
    (source / "pyproject.toml").write_bytes(PYPROJECT_STUB)
    return source


@pytest.fixture
def buildable_source(fake_source: Path) -> Path:
    """fake_source plus the version file the build's version gate reads."""
    (fake_source / "deeptutor").mkdir()
    (fake_source / "deeptutor" / "__version__.py").write_text(
        '__version__ = "9.9.9-test"\n', encoding="utf-8"
    )
    return fake_source


@pytest.fixture
def stub_build_py(tmp_path: Path) -> Path:
    """An existing file standing in for the cp312 build interpreter."""
    build_py = tmp_path / "python-build.exe"
    build_py.write_bytes(b"stub")
    return build_py


@pytest.fixture
def stub_py_exe(tmp_path: Path) -> Path:
    """An existing file standing in for the staging interpreter.

    Never executed: smoke_test's subprocess.run is faked in every unit test.
    """
    py_exe = tmp_path / "staging-python.exe"
    py_exe.write_bytes(b"stub")
    return py_exe


@pytest.fixture
def build_py_patched(monkeypatch, stub_build_py: Path) -> Path:
    monkeypatch.setattr(br, "BUILD_PY", stub_build_py)
    return stub_build_py


# ---------------------------------------------------------------------------
# subprocess fakes
# ---------------------------------------------------------------------------


def _record_pip_runs(monkeypatch, returncodes: list[int]) -> list[list[str]]:
    """Replace br.subprocess.run; capture every pip command line.

    ``returncodes`` is consumed in order; the last entry repeats for any
    further attempts (so [1, 0] models the isolated-build retry succeeding).
    """
    calls: list[list[str]] = []
    attempts = {"n": 0}

    def fake_run(cmd, *args, **kwargs):
        index = min(attempts["n"], len(returncodes) - 1)
        attempts["n"] += 1
        calls.append([str(part) for part in cmd])
        return SimpleNamespace(returncode=returncodes[index], stdout="", stderr="")

    monkeypatch.setattr(br.subprocess, "run", fake_run)
    return calls


def _exec_probe_inprocess(code: str, missing: set[str]) -> tuple[int, str, str]:
    """Run smoke_test's generated probe code in-process, hermetically.

    Emulates what the staging python does with ``-c <probe>``: executes the
    exact code the gate generates, with ``importlib.util.find_spec`` patched
    so the verdict does not depend on the packages installed in *this*
    interpreter (the real-environment case is covered by the e2e test below).
    Returns (returncode, stdout, stderr) like a subprocess result.
    """
    import importlib.util as ilu

    real_find_spec = ilu.find_spec

    def fake_find_spec(name, *args, **kwargs):
        return None if name in missing else SimpleNamespace(name=name)

    ilu.find_spec = fake_find_spec
    captured = io.StringIO()
    rc, err = 0, ""
    try:
        with contextlib.redirect_stdout(captured):
            exec(compile(code, "<build-runtime-probe>", "exec"), {"__name__": "__probe__"})
    except BaseException as exc:  # noqa: BLE001 - mirrors a crashing -c process
        rc, err = 1, f"{type(exc).__name__}: {exc}"
    finally:
        ilu.find_spec = real_find_spec
    return rc, captured.getvalue(), err


def _patch_smoke_runs(
    monkeypatch,
    *,
    missing: set[str] = frozenset(),
    probe_rc: int | None = None,
    probe_stdout: str | None = None,
    probe_stderr: str = "boom",
    record: list[list[str]] | None = None,
) -> None:
    """Intercept smoke_test's two subprocess calls.

    The import-check is answered with a canned success; the find_spec probe
    (recognised by its content) is either canned (when ``probe_rc`` or
    ``probe_stdout`` is given) or executed in-process via
    ``_exec_probe_inprocess`` with the requested missing-module set.
    """

    def fake_run(cmd, *args, **kwargs):
        cmd = [str(part) for part in cmd]
        if record is not None:
            record.append(cmd)
        code = cmd[2] if len(cmd) > 2 else ""
        if "find_spec" in code:
            if probe_stdout is not None or probe_rc is not None:
                return SimpleNamespace(
                    returncode=probe_rc if probe_rc is not None else 0,
                    stdout=probe_stdout or "",
                    stderr=probe_stderr,
                )
            out_rc, out, err = _exec_probe_inprocess(code, set(missing))
            return SimpleNamespace(returncode=out_rc, stdout=out, stderr=err)
        return SimpleNamespace(returncode=0, stdout="runtime imports OK: stub", stderr="")

    monkeypatch.setattr(br.subprocess, "run", fake_run)


# ---------------------------------------------------------------------------
# A. install_deeptutor: the pip target string
# ---------------------------------------------------------------------------


class TestInstallDeeptutorPipTarget:
    """pip target must be `<local-source>[math-animator]`, never a real install."""

    def test_full_install_targets_local_source_with_extra(self, fake_source, build_py_patched, monkeypatch):
        target = fake_source.parent / "staging"
        calls = _record_pip_runs(monkeypatch, [0])

        br.install_deeptutor(target, fake_source)  # default extras=PIP_EXTRAS

        assert len(calls) == 1  # success → no isolated retry
        cmd = calls[0]
        assert cmd[-1] == f"{fake_source}[math-animator]"
        assert "--no-deps" not in cmd  # full path installs the dependency tree
        assert "--no-build-isolation" in cmd  # fast builder tried first
        dest = cmd[cmd.index("--target") + 1]
        assert dest == str(target / "Lib" / "site-packages")  # Windows layout

    def test_no_deps_fast_path_keeps_extras_and_flag(self, fake_source, build_py_patched, monkeypatch):
        target = fake_source.parent / "staging"
        calls = _record_pip_runs(monkeypatch, [0])

        br.install_deeptutor(target, fake_source, with_deps=False)

        cmd = calls[0]
        assert cmd[-1] == f"{fake_source}[math-animator]"
        assert "--no-deps" in cmd

    def test_empty_extras_fall_back_to_plain_source_path(self, fake_source, build_py_patched, monkeypatch):
        target = fake_source.parent / "staging"
        calls = _record_pip_runs(monkeypatch, [0])

        br.install_deeptutor(target, fake_source, extras=())

        assert calls[0][-1] == str(fake_source)
        assert "[" not in calls[0][-1]

    def test_multiple_extras_are_joined_with_comma(self, fake_source, build_py_patched, monkeypatch):
        target = fake_source.parent / "staging"
        calls = _record_pip_runs(monkeypatch, [0])

        br.install_deeptutor(target, fake_source, extras=("alpha", "beta"))

        assert calls[0][-1] == f"{fake_source}[alpha,beta]"

    def test_retry_after_isolation_failure_keeps_spec_and_no_deps(
        self, fake_source, build_py_patched, monkeypatch
    ):
        target = fake_source.parent / "staging"
        calls = _record_pip_runs(monkeypatch, [1, 0])

        br.install_deeptutor(target, fake_source, with_deps=False)

        assert len(calls) == 2
        for cmd in calls:
            assert cmd[-1] == f"{fake_source}[math-animator]"
            assert "--no-deps" in cmd
        assert "--no-build-isolation" in calls[0]
        assert "--no-build-isolation" not in calls[1]

    def test_both_pip_attempts_failing_aborts_the_build(self, fake_source, build_py_patched, monkeypatch):
        target = fake_source.parent / "staging"
        _record_pip_runs(monkeypatch, [1, 1])

        with pytest.raises(SystemExit, match="pip install deeptutor failed"):
            br.install_deeptutor(target, fake_source)


# ---------------------------------------------------------------------------
# B. dependencies_signature: extras must invalidate old state files
# ---------------------------------------------------------------------------


class TestDependenciesSignature:
    def test_formula_is_pyproject_bytes_plus_extras_marker(self, fake_source):
        expected = hashlib.sha256(
            PYPROJECT_STUB + b"\0extras=" + ",".join(br.PIP_EXTRAS).encode("utf-8")
        ).hexdigest()
        assert br.dependencies_signature(fake_source) == expected

    def test_toggling_extras_changes_the_signature(self, fake_source, monkeypatch):
        with_extras = br.dependencies_signature(fake_source)
        monkeypatch.setattr(br, "PIP_EXTRAS", ())
        without = br.dependencies_signature(fake_source)
        assert with_extras != without

    def test_signature_never_equals_the_bare_pyproject_hash(self, fake_source, monkeypatch):
        """Old .source-state.json held sha256(pyproject bytes); it must go stale.

        Guarantees one full reinstall after upgrading this script, whichever
        extras value is in force.
        """
        bare = hashlib.sha256(PYPROJECT_STUB).hexdigest()
        monkeypatch.setattr(br, "PIP_EXTRAS", ())
        assert br.dependencies_signature(fake_source) != bare
        monkeypatch.setattr(br, "PIP_EXTRAS", ("math-animator",))
        assert br.dependencies_signature(fake_source) != bare

    def test_signature_is_deterministic_for_identical_input(self, fake_source):
        assert br.dependencies_signature(fake_source) == br.dependencies_signature(fake_source)

    def test_changed_pyproject_content_changes_the_signature(self, fake_source, monkeypatch):
        monkeypatch.setattr(br, "PIP_EXTRAS", ())
        first = br.dependencies_signature(fake_source)
        (fake_source / "pyproject.toml").write_bytes(PYPROJECT_STUB + b"\n# bump\n")
        second = br.dependencies_signature(fake_source)
        assert first != second

    def test_missing_pyproject_returns_the_empty_marker(self, tmp_path):
        assert br.dependencies_signature(tmp_path) == ""


# ---------------------------------------------------------------------------
# C. smoke_test: the extras gate
# ---------------------------------------------------------------------------


class TestSmokeTestExtrasGate:
    def test_gate_passes_when_all_declared_modules_present(self, stub_py_exe, monkeypatch, capsys):
        _patch_smoke_runs(monkeypatch, missing=set())

        br.smoke_test(stub_py_exe)  # must not raise

        out = capsys.readouterr().out
        assert "extras gate OK" in out
        assert "manim" in out  # the OK line names the probed modules

    def test_missing_manim_fails_gate_naming_module_and_extra(self, stub_py_exe, monkeypatch):
        _patch_smoke_runs(monkeypatch, missing={"manim"})

        with pytest.raises(SystemExit) as excinfo:
            br.smoke_test(stub_py_exe)

        message = str(excinfo.value)
        assert "EXTRAS GATE FAILED" in message
        assert "manim" in message
        assert "math-animator" in message

    @pytest.mark.parametrize("absent", ["scipy", "av"])
    def test_each_transitive_dependency_is_gated_too(self, stub_py_exe, monkeypatch, absent):
        _patch_smoke_runs(monkeypatch, missing={absent})

        with pytest.raises(SystemExit) as excinfo:
            br.smoke_test(stub_py_exe)

        message = str(excinfo.value)
        assert absent in message
        assert "math-animator" in message

    def test_empty_pip_extras_skip_the_gate_entirely(self, stub_py_exe, monkeypatch):
        record: list[list[str]] = []
        monkeypatch.setattr(br, "PIP_EXTRAS", ())
        monkeypatch.setattr(br, "EXTRA_SMOKE_MODULES", {})
        _patch_smoke_runs(monkeypatch, record=record)

        br.smoke_test(stub_py_exe)  # no SystemExit despite zero declarations

        assert len(record) == 1  # only the import check ran
        assert "find_spec" not in record[0][2]  # the gate probe never ran

    def test_extra_without_declaration_is_a_config_error(self, stub_py_exe, monkeypatch):
        record: list[list[str]] = []
        monkeypatch.setattr(br, "PIP_EXTRAS", ("mystery-extra",))
        _patch_smoke_runs(monkeypatch, record=record)

        with pytest.raises(SystemExit) as excinfo:
            br.smoke_test(stub_py_exe)

        message = str(excinfo.value)
        assert "EXTRAS GATE CONFIG ERROR" in message
        assert "mystery-extra" in message
        assert len(record) == 1  # config error fires before any probing

    def test_crashing_probe_fails_the_gate(self, stub_py_exe, monkeypatch):
        _patch_smoke_runs(monkeypatch, probe_rc=1, probe_stderr="SyntaxError: oops")

        with pytest.raises(SystemExit, match="probe script errored"):
            br.smoke_test(stub_py_exe)

    def test_unparsable_probe_output_fails_the_gate(self, stub_py_exe, monkeypatch):
        _patch_smoke_runs(monkeypatch, probe_stdout="definitely not json")

        with pytest.raises(SystemExit, match="cannot parse probe output"):
            br.smoke_test(stub_py_exe)


class TestRealInterpreterEndToEnd:
    def test_smoke_gate_passes_on_this_interpreter(self, monkeypatch, capsys):
        """Run the real gate, subprocesses included, on the live interpreter.

        This box has manim/scipy/av installed, so the full path — real -c
        import check, real find_spec probe — must come out green.
        """
        absent = [
            mod for mod in MATH_ANIMATOR_MODULES
            if importlib.util.find_spec(mod) is None
        ]
        if absent:
            pytest.skip(f"extras not installed in this interpreter: {absent}")
        monkeypatch.chdir(REPO_ROOT)  # lets the child import deeptutor/ from the repo

        br.smoke_test(Path(sys.executable))  # must not raise

        assert "extras gate OK" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# build() wiring: signature invalidation must drive the reinstall decision
# ---------------------------------------------------------------------------


class _BuildRunFaker:
    """Routes build()'s subprocess calls: pip / version / smoke / probe."""

    def __init__(self) -> None:
        self.pip_cmds: list[list[str]] = []

    def __call__(self, cmd, *args, **kwargs):
        cmd = [str(part) for part in cmd]
        if cmd[1:3] == ["-m", "pip"]:
            self.pip_cmds.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        code = cmd[2] if len(cmd) > 2 else ""
        if "find_spec" in code:
            out_rc, out, err = _exec_probe_inprocess(code, set())
            return SimpleNamespace(returncode=out_rc, stdout=out, stderr=err)
        if "__version__" in code:
            return SimpleNamespace(returncode=0, stdout="9.9.9-test\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="runtime imports OK: stub", stderr="")


def _stage_fake_runtime(monkeypatch, tmp_path: Path) -> Path:
    """Redirect every build() filesystem touchpoint into tmp_path.

    The staging tree looks "already prepared" (python.exe + a python*.dll +
    node.exe stubs), so build() skips all downloads/extracts. UCRT copying is
    patched out — it only thrashes tmp_path with system DLLs.
    """
    stage = tmp_path / "runtime-build"
    py_dir = stage / "staging" / "python"
    node_dir = stage / "staging" / "node"
    py_dir.mkdir(parents=True)
    node_dir.mkdir(parents=True)
    (py_dir / "python.exe").write_bytes(b"stub")
    (py_dir / "python312.dll").write_bytes(b"stub")
    (node_dir / "node.exe").write_bytes(b"stub")
    monkeypatch.setattr(br, "STAGING_PY", py_dir)
    monkeypatch.setattr(br, "STAGING_NODE", node_dir)
    monkeypatch.setattr(br, "DIST", tmp_path / "dist")
    monkeypatch.setattr(br, "SOURCE_STATE_FILE", stage / ".source-state.json")
    monkeypatch.setattr(br, "copy_ucrt_into_python", lambda target: 0)
    return py_dir


def _site_packages(py_dir: Path) -> Path:
    return py_dir / "Lib" / "site-packages"


def _seed_state(source_root: Path, py_dir: Path, *, deps_sig: str) -> None:
    """Write a .source-state.json whose fingerprint matches the source now."""
    br.SOURCE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    br.SOURCE_STATE_FILE.write_text(
        json.dumps({
            "mode": "fast",
            "fingerprint": br.source_fingerprint(source_root, strict=False),
            "deps_sig": deps_sig,
        }),
        encoding="utf-8",
    )


class TestBuildWiring:
    """The signature change must translate into build()-level decisions."""

    def test_fresh_build_installs_full_deps_with_extras(
        self, tmp_path, monkeypatch, buildable_source, stub_build_py
    ):
        _stage_fake_runtime(monkeypatch, tmp_path)
        monkeypatch.setattr(br, "BUILD_PY", stub_build_py)
        faker = _BuildRunFaker()
        monkeypatch.setattr(br.subprocess, "run", faker)

        br.build(make_zip=False, source_root=buildable_source)

        assert len(faker.pip_cmds) == 1
        cmd = faker.pip_cmds[0]
        assert cmd[-1] == f"{buildable_source}[math-animator]"
        assert "--no-deps" not in cmd
        state = json.loads(br.SOURCE_STATE_FILE.read_text(encoding="utf-8"))
        assert state["deps_sig"] == br.dependencies_signature(buildable_source)

    def test_old_bare_hash_state_is_invalidated_into_a_full_reinstall(
        self, tmp_path, monkeypatch, buildable_source, stub_build_py
    ):
        """A pre-change .source-state.json (bare pyproject hash) forces full deps."""
        py_dir = _stage_fake_runtime(monkeypatch, tmp_path)
        monkeypatch.setattr(br, "BUILD_PY", stub_build_py)
        (_site_packages(py_dir) / "deeptutor").mkdir(parents=True)
        bare_hash = hashlib.sha256(PYPROJECT_STUB).hexdigest()
        _seed_state(buildable_source, py_dir, deps_sig=bare_hash)
        faker = _BuildRunFaker()
        monkeypatch.setattr(br.subprocess, "run", faker)

        br.build(make_zip=False, source_root=buildable_source)

        assert len(faker.pip_cmds) == 1
        assert "--no-deps" not in faker.pip_cmds[0]  # full reinstall, not fast path
        assert faker.pip_cmds[0][-1] == f"{buildable_source}[math-animator]"

    def test_current_state_with_unchanged_deps_skips_pip(
        self, tmp_path, monkeypatch, buildable_source, stub_build_py
    ):
        py_dir = _stage_fake_runtime(monkeypatch, tmp_path)
        monkeypatch.setattr(br, "BUILD_PY", stub_build_py)
        (_site_packages(py_dir) / "deeptutor").mkdir(parents=True)
        _seed_state(buildable_source, py_dir, deps_sig=br.dependencies_signature(buildable_source))
        faker = _BuildRunFaker()
        monkeypatch.setattr(br.subprocess, "run", faker)

        br.build(make_zip=False, source_root=buildable_source)

        assert faker.pip_cmds == []  # nothing changed → no pip at all

    def test_code_only_change_keeps_fast_path_and_still_passes_extras(
        self, tmp_path, monkeypatch, buildable_source, stub_build_py
    ):
        """Fingerprint bump with same deps_sig → --no-deps, extras still attached."""
        py_dir = _stage_fake_runtime(monkeypatch, tmp_path)
        monkeypatch.setattr(br, "BUILD_PY", stub_build_py)
        (_site_packages(py_dir) / "deeptutor").mkdir(parents=True)
        _seed_state(buildable_source, py_dir, deps_sig=br.dependencies_signature(buildable_source))
        faker = _BuildRunFaker()
        monkeypatch.setattr(br.subprocess, "run", faker)

        # code-only edit: inside the deeptutor package, pyproject untouched
        (buildable_source / "deeptutor" / "newly_added.py").write_text("x = 1\n", encoding="utf-8")

        br.build(make_zip=False, source_root=buildable_source)

        assert len(faker.pip_cmds) == 1
        cmd = faker.pip_cmds[0]
        assert "--no-deps" in cmd
        assert cmd[-1] == f"{buildable_source}[math-animator]"


# ---------------------------------------------------------------------------
# D. Regression: constants, config invariants, byte-compilation
# ---------------------------------------------------------------------------


class TestRegression:
    def test_build_runtime_byte_compiles(self, tmp_path):
        py_compile.compile(str(BUILD_RUNTIME_PY), cfile=str(tmp_path / "br.pyc"), doraise=True)

    def test_every_pip_extra_has_smoke_modules_declared(self):
        for extra in br.PIP_EXTRAS:
            modules = br.EXTRA_SMOKE_MODULES.get(extra)
            assert modules, f"extra {extra!r} lacks an EXTRA_SMOKE_MODULES declaration"
            assert all(isinstance(mod, str) and mod for mod in modules)

    def test_smoke_module_config_has_no_dead_entries(self):
        """EXTRA_SMOKE_MODULES keys must map 1:1 onto PIP_EXTRAS (no dead config)."""
        assert set(br.EXTRA_SMOKE_MODULES) == set(br.PIP_EXTRAS)

    def test_pip_extras_reference_real_pyproject_extras(self):
        """Shipped extras must exist in the repo's pyproject.toml."""
        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        declared = data["project"]["optional-dependencies"]
        for extra in br.PIP_EXTRAS:
            assert extra in declared, f"{extra!r} is not declared in pyproject.toml"

    def test_math_animator_gate_covers_manim_stack(self):
        assert br.PIP_EXTRAS == ("math-animator",)
        assert br.EXTRA_SMOKE_MODULES["math-animator"] == MATH_ANIMATOR_MODULES
