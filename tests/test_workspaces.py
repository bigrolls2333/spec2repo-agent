import hashlib
import zipfile
from uuid import uuid4

import pytest

from spec2repo.storage.execution import Lease
from spec2repo.storage.workspaces import SnapshotError, Workspaces


def test_snapshot_restore_isolated_and_tamper_detected(tmp_path):
    spaces = Workspaces(tmp_path)
    lease = Lease(str(uuid4()), 1, "one", "a" * 64)
    old = spaces.create(lease)
    (old / "state.txt").write_text("committed")
    digest = spaces.snapshot(old)
    (old / "state.txt").write_text("late mutation")
    new = spaces.workspace(lease.run_id, 2)
    new.mkdir()
    spaces.restore(digest, new)
    assert (new / "state.txt").read_text() == "committed"
    snapshot = spaces.verify(digest)
    snapshot.chmod(0o666)
    snapshot.write_bytes(b"tampered")
    with pytest.raises(SnapshotError, match="hash mismatch"):
        spaces.verify(digest)


@pytest.mark.parametrize(
    "name", ["../escape", "/absolute", "C:/secret", "dir\\escape", "NUL", "x/../y"]
)
def test_archive_paths_cannot_escape(tmp_path, name):
    spaces = Workspaces(tmp_path)
    archive = tmp_path / "malicious.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(name, "bad")
    if "\\" in name:
        # Windows ZipInfo normalizes names while writing; restore the raw ZIP name.
        archive.write_bytes(archive.read_bytes().replace(name.replace("\\", "/").encode(), name.encode()))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.rename(spaces.snapshots / f"{digest}.zip")
    workspace = spaces.create(Lease(str(uuid4()), 1, "x", "a" * 64))
    with pytest.raises(SnapshotError):
        spaces.restore(digest, workspace)
    assert list(workspace.iterdir()) == []


def test_snapshot_quota_and_outside_source_rejected(tmp_path):
    spaces = Workspaces(tmp_path / "runtime")
    with pytest.raises(SnapshotError):
        spaces.snapshot(tmp_path)
    workspace = spaces.create(Lease(str(uuid4()), 1, "x", "a" * 64))
    (workspace / "large.txt").write_bytes(b"12345")
    spaces.MAX_BYTES = 4
    with pytest.raises(SnapshotError, match="quota"):
        spaces.snapshot(workspace)
    assert list(spaces.snapshots.iterdir()) == []
