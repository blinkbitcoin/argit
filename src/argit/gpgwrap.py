"""GPG subprocess wrapper.

Parses `gpg --with-colons` output per the DETAILS file format (pub/uid/fpr
records). All operations honor a 30s timeout; FileNotFoundError is mapped to
the install-line first-touch error.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ArgitError

GPG_TIMEOUT_SEC = 30


@dataclass
class GpgKey:
    fpr: str
    uids: list[str] = field(default_factory=list)
    capability: str = ""


class GpgWrap:
    @staticmethod
    def _run(args: list[str], *, stdin: str | None = None,
             check: bool = True) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["gpg", *args],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=GPG_TIMEOUT_SEC,
                check=check,
            )
        except FileNotFoundError as exc:
            raise ArgitError.cmd_not_found("gpg", "brew install gnupg", "apt install gnupg") from exc
        except subprocess.TimeoutExpired as exc:
            raise ArgitError(
                f"gpg {args[0] if args else '<no-args>'} timed out after {GPG_TIMEOUT_SEC}s",
                "If pinentry is blocking, run `gpg --decrypt` once interactively to cache the passphrase.",
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise ArgitError(
                f"gpg {' '.join(args)} failed (exit {exc.returncode}): {exc.stderr.strip() or exc.stdout.strip()}",
                "run `argit doctor` to audit GPG state",
            ) from exc

    def list_keys(self) -> list[GpgKey]:
        cp = self._run(["--with-colons", "--list-keys"])
        return _parse_colons(cp.stdout)

    def is_key_imported(self, fpr: str) -> bool:
        target = fpr.replace(" ", "").upper()
        for k in self.list_keys():
            if k.fpr.upper() == target:
                return True
        return False

    def import_key(self, asc_path: Path) -> None:
        if not asc_path.is_file():
            raise ArgitError(
                f"GPG public-key file not found: {asc_path}",
                "verify the argit installation; the bundled key should ship inside the package",
            )
        self._run(["--import", str(asc_path)])

    def set_ownertrust(self, fpr: str, level: int) -> None:
        """Set ownertrust without launching gpg --edit-key.

        GPG TRUST_VALUES (`g10/trustdb.h`): 1=Unknown, 2=Undefined, 3=Marginal,
        4=Full, 5=Ultimate. Caller passes the GPG numeric directly.
        """
        line = f"{fpr.upper()}:{level}:\n"
        self._run(["--import-ownertrust"], stdin=line)

    def list_personal_keys(self, exclude_fpr: str) -> list[GpgKey]:
        target = exclude_fpr.replace(" ", "").upper()
        return [k for k in self.list_keys() if k.fpr.upper() != target]


def _parse_colons(text: str) -> list[GpgKey]:
    """Parse `gpg --with-colons --list-keys` output.

    Records are line-oriented; fields separated by `:`. Relevant types:
    `pub` (public key) — capability in field 12 — followed by `fpr` (full
    fingerprint in field 10) and one or more `uid` records (uid in field 10).
    """
    keys: list[GpgKey] = []
    current: GpgKey | None = None
    for line in text.splitlines():
        if not line:
            continue
        fields = line.split(":")
        rectype = fields[0]
        if rectype == "pub":
            current = GpgKey(fpr="", capability=fields[11] if len(fields) > 11 else "")
            keys.append(current)
        elif rectype == "fpr" and current is not None and not current.fpr:
            if len(fields) > 9:
                current.fpr = fields[9]
        elif rectype == "uid" and current is not None:
            if len(fields) > 9 and fields[9]:
                current.uids.append(fields[9])
    return [k for k in keys if k.fpr]
