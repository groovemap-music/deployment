"""Verify every locked performance-test dependency has reviewed license metadata."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
lock_text = (ROOT / "tests/perftest/requirements.lock").read_text()
locked = set(re.findall(r"^([a-z0-9][a-z0-9._-]*)==", lock_text, flags=re.MULTILINE))
licenses = json.loads((ROOT / "tests/perftest/dependency-licenses.json").read_text())

assert locked == set(licenses), f"license manifest mismatch: missing={sorted(locked - set(licenses))}, extra={sorted(set(licenses) - locked)}"
blocked = {name: license_id for name, license_id in licenses.items() if license_id.startswith(("AGPL-", "GPL-"))}
assert not blocked, f"blocked dependency licenses: {blocked}"
