import subprocess
import sys

from fqdb.config import Config
from test_contracts import setup_store


def test_opted_out_offline_query_does_not_create_service(data_root):
    setup_store(data_root)
    assert not (data_root / ".state/state/service.json").exists()
    script = """
import fqdb
import sys
fqdb.init(data_dir=sys.argv[1])
p = fqdb.get_price("000100.SZ", "2026-11-12", "2026-11-13", fields=["close"])
assert p.shape == (2, 1)
assert p.attrs["synthetic"]
print("offline query passed")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(data_root)], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr
    assert "passed" in result.stdout
    assert not (Config(data_root).root / ".state/state/service.json").exists()
