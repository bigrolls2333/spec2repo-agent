"""Re-run generated developer tests from a fresh snapshot, never formal acceptance."""

import argparse
import json
import threading
from pathlib import Path
from uuid import uuid4

from spec2repo.sandbox.docker import CommandFailed, DockerSandbox
from spec2repo.storage.execution import Lease
from spec2repo.storage.workspaces import Workspaces

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    spaces = Workspaces(ROOT / ".runtime" / "live-agent")
    lease = Lease(str(uuid4()), 1, "developer-test-check", "a" * 64)
    workspace = spaces.create(lease)
    spaces.restore(report["snapshot_hash"], workspace)
    sandbox = DockerSandbox(spaces, image=report["image_id"])
    try:
        output = sandbox.run(
            lease,
            ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            authorized=lambda: None,
            stop=threading.Event(),
            timeout=30,
        )
        code = 0
    except CommandFailed as exc:
        code, output = exc.exit_code, exc.output
    evidence = {
        "snapshot_hash": report["snapshot_hash"],
        "image_id": sandbox.image,
        "exit_code": code,
        "output": output[:8000],
        "scope": "Generated developer tests in a new restored workspace; not independent acceptance",
    }
    destination = args.report.with_name(args.report.stem + "-developer-check.json")
    destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output[:8000])
    return code


if __name__ == "__main__":
    raise SystemExit(main())
