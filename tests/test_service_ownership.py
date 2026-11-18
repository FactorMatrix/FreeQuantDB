import subprocess
import sys
import time

from fqdb.service.client import Client
from test_contracts import setup_store


def test_second_service_rejected_and_provider_requires_manual_activation_after_restart(data_root):
    setup_store(data_root)
    c = Client(data_root)
    ep = c.ensure()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "fqdb.service.daemon", "--data-dir", str(data_root)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode != 0 and "already owns" in result.stderr
        script = "import fqdb; import sys; fqdb.init(sys.argv[1]); fqdb.provider.start()"
        subprocess.run([sys.executable, "-c", script, str(data_root)], check=True, timeout=20)
        assert c.get("/api/status")["provider"] is True
        c.call("shutdown")
        end = time.monotonic() + 15
        while c.endpoint() and time.monotonic() < end:
            time.sleep(0.2)
        new = c.ensure()
        assert new["pid"] != ep["pid"]
        assert c.get("/api/status")["provider"] is False
        c.call("provider_start")
        assert c.get("/api/status")["provider"] is True
        c.call("provider_stop")
        assert c.get("/api/status")["provider"] is False
    finally:
        if c.endpoint():
            c.call("shutdown")
