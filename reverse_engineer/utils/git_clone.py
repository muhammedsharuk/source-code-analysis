"""Safe cloning of a (possibly private) repo URL into a destination directory.

Mirrors `zip_extract.py`'s stance on untrusted input: this URL is
caller-supplied and *this server* does the fetching, so the two failure
modes to guard against are SSRF (the URL resolves to an internal/private
address this container can reach) and unbounded resource use (a huge repo,
or a clone that hangs). A third concern unique to this path is credential
hygiene -- a token, when given, must never touch disk or a process's argv.
"""

import ipaddress
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

MAX_CLONE_BYTES = 500 * 1024 * 1024  # matches zip_extract.MAX_UNCOMPRESSED_BYTES
CLONE_TIMEOUT_SECONDS = 600

# Comma-separated allowlist of hosts this service may clone from (e.g.
# "github.com,gitlab.example.com"). Empty (the default) means any public
# host is allowed -- the private-IP check below still applies regardless.
_ALLOWED_HOSTS_ENV = "GIT_CLONE_ALLOWED_HOSTS"


class UnsafeCloneError(ValueError):
    """Raised when a repo URL or clone fails a safety check; never partially left behind."""


def _assert_public_host(host: str) -> None:
    allowed = {h.strip().lower() for h in os.environ.get(_ALLOWED_HOSTS_ENV, "").split(",") if h.strip()}
    if allowed and host.lower() not in allowed:
        raise UnsafeCloneError(f"Host {host!r} is not in the configured allowlist.")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise UnsafeCloneError(f"Could not resolve host {host!r}: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise UnsafeCloneError(f"Host {host!r} resolves to a non-public address ({ip}); refusing to clone.")


def _validate_url(repo_url: str) -> str:
    parsed = urlparse(repo_url)
    if parsed.scheme != "https":
        raise UnsafeCloneError("Only https:// repository URLs are supported.")
    if not parsed.hostname:
        raise UnsafeCloneError("Repository URL has no host.")
    if parsed.username or parsed.password:
        raise UnsafeCloneError("Do not embed credentials in the repository URL; use the credential fields instead.")
    _assert_public_host(parsed.hostname)
    return repo_url


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _rmtree_force(path: Path) -> None:
    """`shutil.rmtree`, but clears the read-only bit and retries on failure.

    git marks pack/loose-object files read-only. On POSIX that doesn't block
    deletion (unlink only needs write permission on the *directory*), but on
    Windows the file's own read-only attribute blocks it outright -- and a
    plain `ignore_errors=True` would silently leave `.git` behind instead of
    surfacing that. Handling it explicitly keeps the "no `.git` survives a
    clone" guarantee true on every platform, not just the one this was
    tested on last.
    """

    def _on_error(func, target, _exc_info):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onerror=_on_error)


def safe_clone_repo(repo_url: str, dest_dir: Path, *, username: str | None = None, token: str | None = None) -> None:
    """Shallow-clone `repo_url` into `dest_dir`.

    `dest_dir` is only ever handed a fresh, empty directory by the caller
    (see `run_manager.py`) -- on any failure here, nothing is cleaned up
    inside this function; the caller removes the whole run directory, same
    as it does for a rejected zip upload.

    `.git` is stripped from a successful clone before returning: the
    analysis pipeline only needs the working tree, and this guarantees
    nothing about how the repo was fetched -- including any credential git
    itself might otherwise have cached in `.git/config` -- survives on disk.
    """
    repo_url = _validate_url(repo_url)
    dest_dir = dest_dir.resolve()

    env = dict(os.environ)
    askpass_path: Path | None = None
    if token:
        # A GIT_ASKPASS script that reads the token from an env var -- never
        # from argv (visible to anything that can list this process's
        # command line) and never written into `.git/config` the way
        # embedding it in the URL would.
        fd, askpass_name = tempfile.mkstemp(prefix="git-askpass-", suffix=".py")
        askpass_path = Path(askpass_name)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(
                f"#!{sys.executable}\n"
                "import os, sys\n"
                "prompt = sys.argv[-1].lower()\n"
                "sys.stdout.write(os.environ['GIT_CLONE_TOKEN'] if 'password' in prompt "
                "else os.environ.get('GIT_CLONE_USERNAME', 'x-access-token'))\n"
            )
        askpass_path.chmod(0o700)
        env["GIT_ASKPASS"] = str(askpass_path)
        env["GIT_CLONE_TOKEN"] = token
        env["GIT_CLONE_USERNAME"] = username or "x-access-token"
        env["GIT_TERMINAL_PROMPT"] = "0"

    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "--single-branch", "--no-tags", repo_url, str(dest_dir)],
            env=env,
            capture_output=True,
            text=True,
            timeout=CLONE_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            # git's stderr can be long (and, if something upstream ever regresses on the
            # embedded-credential rule, could echo the URL back) -- truncate defensively.
            raise UnsafeCloneError(f"git clone failed: {result.stderr.strip()[-500:]}")

        if _dir_size(dest_dir) > MAX_CLONE_BYTES:
            raise UnsafeCloneError(
                f"Cloned repository exceeds the {MAX_CLONE_BYTES} byte limit even at shallow depth."
            )
    except subprocess.TimeoutExpired as exc:
        raise UnsafeCloneError(f"git clone timed out after {CLONE_TIMEOUT_SECONDS}s.") from exc
    finally:
        if askpass_path is not None:
            askpass_path.unlink(missing_ok=True)
        git_dir = dest_dir / ".git"
        if git_dir.exists():
            _rmtree_force(git_dir)
