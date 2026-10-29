"""Per-attempt directories and bounded, content-addressed workspace snapshots."""

import hashlib
import os
import re
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from uuid import UUID


class SnapshotError(ValueError):
    pass


class Workspaces:
    MAX_BYTES = 16 * 1024 * 1024
    MAX_FILES = 1000

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.attempts = self.root / "attempts"
        self.snapshots = self.root / "snapshots"
        self.attempts.mkdir(parents=True, exist_ok=True)
        self.snapshots.mkdir(parents=True, exist_ok=True)

    def workspace(self, run_id, generation):
        run_id = str(UUID(run_id))
        if generation < 1:
            raise SnapshotError("Invalid generation")
        result = self.attempts / run_id / str(generation)
        if not result.resolve().is_relative_to(self.attempts.resolve()):
            raise SnapshotError("Workspace escapes attempt root")
        return result

    def create(self, lease):
        target = self.workspace(lease.run_id, lease.generation)
        target.mkdir(parents=True, exist_ok=False)
        target.chmod(0o777)  # Container UID 65534 can write only its mounted attempt.
        return target

    @staticmethod
    def safe_name(name):
        path = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or ":" in name
            or path.is_absolute()
            or any(part in {"..", "."} for part in name.split("/"))
            or any(part.endswith((".", " ")) for part in path.parts)
            or any(
                part.split(".")[0].upper()
                in {
                    "CON",
                    "PRN",
                    "AUX",
                    "NUL",
                    *(f"COM{i}" for i in range(1, 10)),
                    *(f"LPT{i}" for i in range(1, 10)),
                }
                for part in path.parts
            )
        ):
            raise SnapshotError("Unsafe archive path")
        return path

    def snapshot(self, workspace):
        workspace = Path(workspace).resolve()
        if not workspace.is_relative_to(self.attempts.resolve()):
            raise SnapshotError("Snapshot source is outside attempts")
        fd, name = tempfile.mkstemp(dir=self.snapshots, suffix=".tmp")
        os.close(fd)
        temporary = Path(name)
        size = count = 0
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
                for directory, dirs, files in os.walk(workspace, followlinks=False):
                    dirs.sort()
                    for item in sorted(dirs + files):
                        path = Path(directory) / item
                        info = path.lstat()
                        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                            raise SnapshotError("Links are forbidden in snapshots")
                        if stat.S_ISDIR(info.st_mode):
                            continue
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                            raise SnapshotError("Only independent regular files are allowed")
                        size += info.st_size
                        count += 1
                        if size > self.MAX_BYTES or count > self.MAX_FILES:
                            raise SnapshotError("Snapshot quota exceeded")
                        relative = path.relative_to(workspace).as_posix()
                        self.safe_name(relative)
                        with path.open("rb") as source:
                            data = source.read(self.MAX_BYTES + 1)
                        if len(data) != info.st_size or path.stat().st_mtime_ns != info.st_mtime_ns:
                            raise SnapshotError("Workspace changed during snapshot")
                        entry = zipfile.ZipInfo(relative)  # Stable metadata, no host paths/modes.
                        archive.writestr(entry, data)
            digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
            destination = self.snapshots / f"{digest}.zip"
            if destination.exists():
                self.verify(digest)
            else:
                # Publishing a complete file before DB registration leaves at most an orphan.
                os.replace(temporary, destination)
                destination.chmod(0o444)
            return digest
        finally:
            temporary.unlink(missing_ok=True)

    def verify(self, digest):
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SnapshotError("Invalid snapshot hash")
        path = self.snapshots / f"{digest}.zip"
        if path.is_symlink() or path.stat().st_size > self.MAX_BYTES + 1024 * 1024:
            raise SnapshotError("Invalid snapshot file")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise SnapshotError("Snapshot hash mismatch")
        return path

    def restore(self, digest, workspace):
        workspace = Path(workspace).resolve()
        if not workspace.is_relative_to(self.attempts.resolve()) or any(workspace.iterdir()):
            raise SnapshotError("Restore requires a new empty attempt directory")
        path = self.verify(digest)
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > self.MAX_FILES or sum(e.file_size for e in entries) > self.MAX_BYTES:
                raise SnapshotError("Snapshot quota exceeded")
            seen = set()
            for entry in entries:
                self.safe_name(entry.orig_filename)
                relative = self.safe_name(entry.filename)
                if (
                    entry.filename.casefold() in seen
                    or entry.is_dir()
                    or stat.S_ISLNK(entry.external_attr >> 16)
                ):
                    raise SnapshotError("Duplicate or invalid archive entry")
                seen.add(entry.filename.casefold())
                if not (workspace / str(relative)).resolve().is_relative_to(workspace):
                    raise SnapshotError("Archive escapes workspace")
            for entry in entries:
                destination = workspace.joinpath(*PurePosixPath(entry.filename).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.parent.chmod(0o777)
                with destination.open("xb") as output:
                    output.write(archive.read(entry))
                destination.chmod(0o666)
