"""Build and install the public fqdb artifact."""

import configparser
import importlib.util
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile


def test_only_fqdb_is_shipped_and_installed(data_root):
    repo = Path(__file__).resolve().parents[1]
    # Each interpreter builds in its own source tree; concurrent compatibility
    # runs must not share setuptools' build/lib and bdist directories.
    source = data_root / "SYNTHETIC-package-source"
    source.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE", "MANIFEST.in"):
        shutil.copy2(repo / name, source / name)
    for name in ("src", "docs", "examples", "scripts", "tests"):
        if (repo / name).exists():
            shutil.copytree(
                repo / name,
                source / name,
                ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", ".pytest_cache"),
            )
    (source / "tools").mkdir()
    (source / "tools/local_only_example.py").write_text("# Synthetic private local file\n")
    distribution = data_root / "SYNTHETIC-package-output"
    result = subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--wheel", "--no-isolation", "--outdir", str(distribution)],
        cwd=source,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    source_distributions = list(distribution.glob("fqdb-*.tar.gz"))
    assert len(source_distributions) == 1
    with tarfile.open(source_distributions[0]) as archive:
        assert not any("/tools/" in name for name in archive.getnames())
    wheels = list(distribution.glob("fqdb-*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        assert not any(name.startswith("free_quant_db/") for name in names)
        assert not any(name.startswith("tools/") for name in names)
        assert "fqdb/catalog/__init__.py" in names and "fqdb/catalog/models.py" in names
        assert "fqdb/web/__init__.py" in names and "fqdb/web/app.py" in names
        assert "fqdb/api.py" in names and "fqdb/service/daemon.py" in names
        static = source / "src/fqdb/web/static"
        static_files = {p.relative_to(source / "src").as_posix() for p in static.rglob("*") if p.is_file()}
        assert static_files <= set(names)
        metadata = next(name for name in names if name.endswith(".dist-info/METADATA"))
        assert "\nName: fqdb\n" in archive.read(metadata).decode()
        assert "\nRequires-Python: >=3.11\n" in archive.read(metadata).decode()
        assert [name.split("/", 1)[1] for name in names if ".dist-info/licenses/" in name] == [
            "licenses/LICENSE"
        ]
        entry_points = next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
        parser = configparser.ConfigParser()
        parser.read_string(archive.read(entry_points).decode())
        assert dict(parser["console_scripts"]) == {"fqdb": "_fqdb_cli:main"}
        assert "_fqdb_cli.py" in names
    target = data_root / "SYNTHETIC-installed-package"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--target",
            str(target),
            str(wheels[0]),
        ],
        cwd=data_root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    script = r"""
import importlib.util
import json
from pathlib import Path
import sys
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
import fqdb
from fqdb.catalog import models
from fqdb.web import app
from fqdb import query
for module in (fqdb, fqdb.catalog, fqdb.web, models, app, query):
    assert Path(module.__file__).is_relative_to(root), module.__file__
assert importlib.util.find_spec("free_quant_db") is None
assert callable(fqdb.catalog.add) and callable(fqdb.web.run)
assert callable(fqdb.download) and callable(fqdb.subscribe)
print(json.dumps({"package":fqdb.__name__, "version":fqdb.__version__, "origin":fqdb.__file__}))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(target)],
        cwd=data_root,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["package"] == "fqdb"
    script_directory = target / ("Scripts" if os.name == "nt" else "bin")
    assert not (script_directory / ("fqd.exe" if os.name == "nt" else "fqd")).exists()
    if os.name != "nt":
        environment = os.environ | {"PYTHONPATH": str(target)}
        environment.pop("FQDB_AUTOSTART", None)
        result = subprocess.run(
            [str(script_directory / "fqdb"), "--help"],
            cwd=data_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0 and "usage: fqdb" in result.stdout
        assert "gateway" in result.stdout and "catalog" in result.stdout
        assert "serve" not in result.stdout
        assert not (Path(environment["HOME"]) / ".fqdb").exists()
    assert importlib.util.find_spec("free_quant_db") is None
