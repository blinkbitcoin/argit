"""GPG subprocess wrapper.

Parses `gpg --with-colons` output per the DETAILS file format (pub/sec/uid/fpr
records — `pub` and `sec` share the field layout, differing only in which
keyring they come from). All operations honor a 30s timeout; FileNotFoundError
is mapped to the install-line first-touch error.
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
        """Public keyring. Used by `is_key_imported` to verify IT-backup-key
        presence. Do NOT use to decide which keys the operator controls — a
        public-only import would misclassify; use `list_secret_keys` for that."""
        cp = self._run(["--with-colons", "--list-keys"])
        return _parse_colons(cp.stdout)

    def list_secret_keys(self) -> list[GpgKey]:
        """Private keyring — keys whose secret half is present, i.e. keys the
        operator actually controls. This is the right filter for any
        "personal key" decision."""
        cp = self._run(["--with-colons", "--list-secret-keys"])
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
        """Keys the operator controls (secret half present), minus the
        IT backup key. Public-only imports are excluded by construction —
        see `list_secret_keys` docstring."""
        target = exclude_fpr.replace(" ", "").upper()
        return [k for k in self.list_secret_keys() if k.fpr.upper() != target]


def _parse_colons(text: str) -> list[GpgKey]:
    """Parse `gpg --with-colons --list-keys` / `--list-secret-keys` output.

    Records are line-oriented; fields separated by `:`. Relevant types:
    `pub` (public primary key) and `sec` (secret primary key) — same field
    layout, different keyring. Each is followed by `fpr` (full fingerprint
    in field 10) and one or more `uid` records (uid in field 10). Capability
    is field 12 on the primary-key record.
    """
    keys: list[GpgKey] = []
    current: GpgKey | None = None
    for line in text.splitlines():
        if not line:
            continue
        fields = line.split(":")
        rectype = fields[0]
        if rectype in ("pub", "sec"):
            current = GpgKey(fpr="", capability=fields[11] if len(fields) > 11 else "")
            keys.append(current)
        elif rectype == "fpr" and current is not None and not current.fpr:
            if len(fields) > 9:
                current.fpr = fields[9]
        elif rectype == "uid" and current is not None:
            if len(fields) > 9 and fields[9]:
                current.uids.append(fields[9])
    return [k for k in keys if k.fpr]
