"""list_personal_keys must filter from the SECRET keyring, not the public
one. A public-only import (e.g. a colleague's pubkey imported for verifying
signatures) must not count as a "personal key" — only keys the operator
controls do.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from argit.gpgwrap import GpgWrap


# Keyring with two public keys but only one secret half — what you get on a
# machine where the operator imported a colleague's pubkey for signature
# verification but only has their own private key.
PUBLIC_COLONS = """\
pub:u:4096:1:AAAAAAAAAAAAAAAA:1600000000:::u:::scESC::::::23::0:
fpr:::::::::AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA:
uid:u::::1600000000::F00D::OpenClaw Agent wardley <wardley@openclaw.local>::::::::::0:
pub:-:4096:1:BBBBBBBBBBBBBBBB:1610000000:::-:::scESC::::::23::0:
fpr:::::::::BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB:
uid:-::::1610000000::CAFE::Colleague <colleague@example.com>::::::::::0:
"""

SECRET_COLONS = """\
sec:u:4096:1:AAAAAAAAAAAAAAAA:1600000000::::::scESC::+:::23::0:
fpr:::::::::AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA:
uid:u::::1600000000::F00D::OpenClaw Agent wardley <wardley@openclaw.local>::::::::::0:
"""


def _fake_gpg(args, **_kwargs):
    """Mock gpg based on which list-* flag is present."""
    if "--list-secret-keys" in args:
        out = SECRET_COLONS
    elif "--list-keys" in args:
        out = PUBLIC_COLONS
    else:
        out = ""
    # Args[0] is "gpg"; argit.gpgwrap prepends it.
    return subprocess.CompletedProcess(args=args, returncode=0, stdout=out, stderr="")


def test_list_personal_keys_filters_on_secret_keyring():
    """Public keyring has 2 keys; secret keyring has 1. list_personal_keys
    must return only the one with a secret half — no false 'multiple keys'
    error."""
    g = GpgWrap()
    with patch("argit.gpgwrap.subprocess.run", side_effect=_fake_gpg):
        personal = g.list_personal_keys(exclude_fpr="0" * 40)  # excluded key absent
    assert len(personal) == 1
    assert personal[0].fpr == "A" * 40
    assert "wardley" in personal[0].uids[0]


def test_list_personal_keys_excludes_it_backup_even_if_secret_present():
    """If the IT backup key's private half somehow got imported, it must
    still be excluded from personal keys — same exclusion logic as before,
    just applied to the secret-keyring source."""
    secret_with_both = """\
sec:u:4096:1:AAAAAAAAAAAAAAAA:1600000000::::::scESC::+:::23::0:
fpr:::::::::AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA:
uid:u::::1600000000::F00D::Op::::::::::0:
sec:u:4096:1:1107BD74F292CD3E:1500000000::::::scESC::+:::23::0:
fpr:::::::::1107BD74F292CD3EAB0CF59D49F2D3353A88D34E:
uid:u::::1500000000::BEEF::IT Backup::::::::::0:
"""

    def _only_secret(args, **_kwargs):
        if "--list-secret-keys" in args:
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=secret_with_both, stderr="",
            )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    g = GpgWrap()
    with patch("argit.gpgwrap.subprocess.run", side_effect=_only_secret):
        personal = g.list_personal_keys(
            exclude_fpr="1107BD74F292CD3EAB0CF59D49F2D3353A88D34E",
        )
    assert len(personal) == 1
    assert personal[0].fpr == "A" * 40


def test_is_key_imported_still_uses_public_keyring():
    """is_key_imported must keep checking the PUBLIC keyring — the IT backup
    key is distributed as a public key only; private half never on the host."""
    g = GpgWrap()
    with patch("argit.gpgwrap.subprocess.run", side_effect=_fake_gpg):
        # Colleague's pubkey (B…) is public-only. is_key_imported must still
        # find it — because it asks the public keyring.
        assert g.is_key_imported("B" * 40) is True
        # The secret-only fingerprint is also in public. is_key_imported
        # is a public-keyring lookup, so it's True.
        assert g.is_key_imported("A" * 40) is True


def test_list_secret_keys_parses_sec_records():
    """_parse_colons must accept `sec` records with the same field layout as
    `pub` (capability at field 11, fingerprint via following fpr record)."""
    g = GpgWrap()
    with patch("argit.gpgwrap.subprocess.run", side_effect=_fake_gpg):
        keys = g.list_secret_keys()
    assert len(keys) == 1
    assert keys[0].fpr == "A" * 40
    assert keys[0].capability == "scESC"
