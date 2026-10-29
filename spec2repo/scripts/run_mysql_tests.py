"""Opt into destructive tests against the dedicated spec2repo_test schema only."""

import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
environment = os.environ.copy()
for line in (root / ".env.test").read_text(encoding="utf-8").splitlines():
    if line.startswith("SPEC2REPO_TEST_MYSQL_URL="):
        environment["SPEC2REPO_TEST_MYSQL_URL"] = line.split("=", 1)[1]
if not environment.get("SPEC2REPO_TEST_MYSQL_URL"):
    raise SystemExit("Missing test database configuration; run scripts/init_test_env.py")
environment.setdefault("SPEC2REPO_TEST_REDIS_URL", "redis://127.0.0.1:16380/15")
raise SystemExit(
    subprocess.call(
        [sys.executable, "-m", "pytest", *(sys.argv[1:] or ["-q"])],
        cwd=root,
        env=environment,
    )
)
