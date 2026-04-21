"""First-touch error class.

ArgitError carries a (diagnosis, remediation) pair so cli.main can render
them as the two-line "first-touch quality promise" output the product brief
mandates.
"""

from __future__ import annotations


class ArgitError(Exception):
    """Diagnosis + actionable remediation."""

    def __init__(self, diagnosis: str, remediation: str):
        super().__init__(diagnosis)
        self.diagnosis = diagnosis
        self.remediation = remediation

    def __str__(self) -> str:
        return f"{self.diagnosis}\n  → {self.remediation}"

    @classmethod
    def cmd_not_found(cls, cmd: str, mac_install: str, debian_install: str) -> "ArgitError":
        return cls(
            f"{cmd}: command not found",
            f"Install: {mac_install} (Mac) / {debian_install} (Debian)",
        )
