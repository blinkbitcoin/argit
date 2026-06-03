"""PASSWORD_STORE_DIR-scoped wrapper around `pass`.

Every subprocess sets PASSWORD_STORE_DIR to a repo-local path so we never
touch the operator's personal `~/.password-store`. Values are piped via
stdin (never argv — argv leaks through `ps`).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .errors import ArgitError

PASS_TIMEOUT_SEC = 30
PINENTRY_HINT = (
    "If this is a pinentry prompt, run `gpg --decrypt` manually once to cache "
    "the passphrase in gpg-agent, then retry."
)


class PassWrap:
    def __init__(self, store_dir: Path):
        self.store_dir = store_dir

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PASSWORD_STORE_DIR"] = str(self.store_dir)
        # pass otherwise lets gpg prompt on untrusted backup recipients. Argit
        # has already made .gpg-id explicit and doctor verifies key presence.
        existing = env.get("PASSWORD_STORE_GPG_OPTS", "").strip()
        trust_opt = "--trust-model always"
        env["PASSWORD_STORE_GPG_OPTS"] = f"{existing} {trust_opt}".strip() if existing else trust_opt
        return env

    def _run(self, args: list[str], *, stdin: str | None = None,
             check: bool = True, timeout: int = PASS_TIMEOUT_SEC) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["pass", *args],
                input=stdin,
                env=self._env(),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=check,
            )
        except FileNotFoundError as exc:
            raise ArgitError.cmd_not_found("pass", "brew install pass", "apt install pass") from exc
        except subprocess.TimeoutExpired as exc:
            raise ArgitError(
                f"pass {args[0] if args else '<no-args>'} timed out after {timeout}s",
                PINENTRY_HINT,
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise ArgitError(
                f"pass {' '.join(args)} failed (exit {exc.returncode}): {exc.stderr.strip() or exc.stdout.strip()}",
                "run `argit doctor` to audit pass-store state",
            ) from exc

    def has(self, pass_path: str) -> bool:
        try:
            cp = subprocess.run(
                ["pass", "show", pass_path],
                env=self._env(),
                capture_output=True,
                text=True,
                timeout=PASS_TIMEOUT_SEC,
            )
        except FileNotFoundError as exc:
            raise ArgitError.cmd_not_found("pass", "brew install pass", "apt install pass") from exc
        except subprocess.TimeoutExpired as exc:
            raise ArgitError(
                f"pass show timed out after {PASS_TIMEOUT_SEC}s", PINENTRY_HINT,
            ) from exc
        return cp.returncode == 0

    def show(self, pass_path: str) -> str:
        cp = self._run(["show", pass_path])
        # `pass show` prints the secret with a trailing newline; preserve nothing
        # past the first line for typical single-line secrets but return raw for
        # multi-line — the caller (sanitize re-injection) normalizes JSON.
        return cp.stdout.rstrip("\n")

    def insert(self, pass_path: str, value: str) -> None:
        """Idempotent insert: skip the write when the existing value matches.

        Mirrors bot-provisioning-poc/inject_secret.sh — strip trailing
        newlines on both sides before comparison so re-runs don't churn.
        """
        if self.has(pass_path):
            existing = self.show(pass_path)
            if existing.rstrip("\n") == value.rstrip("\n"):
                return
        # `pass insert -m -f`: multi-line, force overwrite. Read from stdin.
        self._run(["insert", "-m", "-f", pass_path], stdin=value if value.endswith("\n") else value + "\n")

    def ls(self) -> list[str]:
        # Use `pass ls` for human listing; for machine listing we walk the store
        # directly because `pass ls` output is tree-formatted and brittle to parse.
        out: list[str] = []
        if not self.store_dir.is_dir():
            return out
        for p in self.store_dir.rglob("*.gpg"):
            rel = p.relative_to(self.store_dir).with_suffix("").as_posix()
            out.append(rel)
        return sorted(out)
