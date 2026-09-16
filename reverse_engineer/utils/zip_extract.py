"""Safe extraction of an uploaded repository zip.

Two things a zip from an untrusted client must never be allowed to do to the
server: write outside the directory we chose for it ("zip-slip", via `../`
entries or absolute paths), or exhaust disk by claiming to be small while
decompressing to something enormous (a "zip bomb"). Both are checked before
a single byte is written.
"""

import zipfile
from pathlib import Path

MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024  # 500MB -- generous for a source repo, not for a bomb
MAX_MEMBERS = 50_000


class UnsafeZipError(ValueError):
    """Raised when an archive fails a safety check; never partially extracted."""


def _is_within(directory: Path, target: Path) -> bool:
    try:
        target.relative_to(directory)
        return True
    except ValueError:
        return False


def safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    """Validate every member of `zip_path`, then extract it into `dest_dir`.

    `dest_dir` is created only after every member has passed validation, so a
    rejected archive never leaves a partially-extracted directory behind.
    """
    dest_dir = dest_dir.resolve()

    with zipfile.ZipFile(zip_path) as zf:
        infolist = zf.infolist()

        if len(infolist) > MAX_MEMBERS:
            raise UnsafeZipError(f"Archive has too many entries ({len(infolist)} > {MAX_MEMBERS}).")

        total_uncompressed = sum(info.file_size for info in infolist)
        if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
            raise UnsafeZipError(
                f"Archive would extract to {total_uncompressed} bytes, "
                f"exceeding the {MAX_UNCOMPRESSED_BYTES} byte limit."
            )

        for info in infolist:
            name = info.filename
            if name.startswith("/") or name.startswith("\\") or ".." in Path(name).parts:
                raise UnsafeZipError(f"Unsafe path in archive: {name!r}")
            member_path = (dest_dir / name).resolve()
            if not _is_within(dest_dir, member_path):
                raise UnsafeZipError(f"Archive entry escapes the extraction directory: {name!r}")

        dest_dir.mkdir(parents=True, exist_ok=True)
        zf.extractall(dest_dir)
