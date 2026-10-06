"""The API process must not load LXD client code. This complements the deployment
boundary (hsm-api is not in the lxd group): even code paths cannot reach pylxd."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def test_api_import_graph_excludes_lxd_and_collector():
    code = (
        "import sys; import hsm.app, hsm.api.containers, hsm.history_client\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] == 'pylxd' or m.startswith(('hsm.integrations', 'hsm.collector', 'hsm.worker')))\n"
        "print(','.join(bad))"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""
