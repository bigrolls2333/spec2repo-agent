"""Create local test credentials once. Never print passwords or connection URLs."""

import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
target = root / ".env.test"
password = secrets.token_hex(24)
try:
    with target.open("x", encoding="utf-8") as output:
        output.write(
            f"MYSQL_TEST_PASSWORD={password}\n"
            f"MYSQL_TEST_ROOT_PASSWORD={secrets.token_hex(24)}\n"
            f"SPEC2REPO_TEST_MYSQL_URL=mysql+pymysql://spec2repo_test:{password}"
            "@127.0.0.1:13307/spec2repo_test?charset=utf8mb4\n"
        )
    print("Created .env.test (git-ignored, local test credentials only)")
except FileExistsError:
    print("Keeping existing .env.test")
