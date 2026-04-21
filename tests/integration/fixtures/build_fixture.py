"""Generate a synthetic OpenClaw fixture from scratch.

Output: tests/integration/fixtures/openclaw/<...>

Every file uses plausible fake values matching the real schema. SQLite DBs
have a minimal schema (1 table, 2-3 rows) and one write-after-open to
generate `-wal` / `-shm` siblings, exercising the live-write handling that
`sqlite3 .backup` is supposed to handle transparently.

Idempotent: re-running overwrites the output directory.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from pathlib import Path

DEFAULT_OUTPUT = Path(__file__).resolve().parent / "openclaw"

FAKE_GH_TOKEN = "ghu_FAKExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxFAKE"
FAKE_TG_TOKEN = "12345:fake-telegram-bot-token-AAAAAAAAA"
FAKE_SLACK_BOT = "xoxb-fake-slack-bot-token-aaaaaaaa"
FAKE_SLACK_APP = "xapp-fake-slack-app-token-aaaaaaaa"
FAKE_GATEWAY = "fake-gateway-bearer-token-1234567890"
FAKE_OPENAI = "sk-proj-FAKExxxxxxxxxxxxxxxxxxxxxxxxxxFAKE"
FAKE_ANTHROPIC = "sk-ant-FAKExxxxxxxxxxxxxxxxxxxxxxxxxxxFAKE"
FAKE_ED25519_PRIV = "-----BEGIN PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIEIFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE\n-----END PRIVATE KEY-----\n"
FAKE_ED25519_PUB_B64 = "FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE="
FAKE_OPERATOR_TOKEN = "operator-bearer-FAKE-1234567890"
FAKE_SOCKET_TOKEN = "exec-approval-socket-FAKE-1234567890"


def _write_json(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _make_sqlite(path: Path, table_sql: str, rows: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    # Open with WAL journaling so that write-after-open creates -wal/-shm.
    con = sqlite3.connect(str(path))
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(table_sql)
        con.executemany(
            f"INSERT INTO t VALUES ({','.join('?' for _ in rows[0])})",
            rows,
        )
        con.commit()
        # one write-after-open to leave WAL/SHM around (use NULL pk to avoid UNIQUE collision)
        nulls = (None,) + rows[0][1:]
        con.execute(f"INSERT INTO t VALUES ({','.join('?' for _ in nulls)})", nulls)
        con.commit()
    finally:
        con.close()


def build(out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    # openclaw.json — sanitize target with multiple secrets + .env subtree
    _write_json(out / "openclaw.json", {
        "gateway": {"auth": {"token": FAKE_GATEWAY}},
        "channels": {
            "telegram": {"botToken": FAKE_TG_TOKEN, "chatId": "-100123456789"},
            "slack": {
                "botToken": FAKE_SLACK_BOT,
                "appToken": FAKE_SLACK_APP,
                "workspace": "fake-workspace",
            },
        },
        "env": {
            "OPENAI_API_KEY": FAKE_OPENAI,
            "ANTHROPIC_API_KEY": FAKE_ANTHROPIC,
        },
        "meta": {"createdAt": "2026-04-01T00:00:00Z"},
    })

    # backup of openclaw.json — exclude pattern openclaw.json.bak*
    (out / "openclaw.json.bak").write_text("{}\n")
    (out / "openclaw.json.bak2").write_text("{}\n")

    # exec-approvals.json — sanitize target (one secret)
    _write_json(out / "exec-approvals.json", {
        "socket": {"token": FAKE_SOCKET_TOKEN, "path": "/var/run/openclaw.sock"},
        "policies": [],
    })

    # update-check.json — data
    _write_json(out / "update-check.json", {
        "lastCheckedAt": "2026-04-01T00:00:00Z",
        "latest": "2026.4.14",
    })

    # agents/main/agent/*.json
    _write_json(out / "agents/main/agent/auth-profiles.json", {
        "profiles": {
            "default": {"token": FAKE_GH_TOKEN, "expiresAt": "2026-12-31T00:00:00Z"},
            "secondary": {"token": FAKE_GH_TOKEN, "expiresAt": "2026-12-31T00:00:00Z"},
        }
    })
    _write_json(out / "agents/main/agent/auth-state.json", {"loginCount": 7, "lastLogin": "2026-04-01T00:00:00Z"})
    _write_json(out / "agents/main/agent/models.json", {
        "providers": {"openai": {"baseUrl": "https://api.openai.com/v1", "apiKey": "codex-app-server"}}
    })

    # agents/main/sessions — excluded (ephemeral)
    (out / "agents/main/sessions").mkdir(parents=True, exist_ok=True)
    (out / "agents/main/sessions/2026-04-01.jsonl").write_text('{"role":"system","content":"ephemeral"}\n')

    # canvas/index.html — data
    (out / "canvas").mkdir(parents=True, exist_ok=True)
    (out / "canvas/index.html").write_text("<!doctype html><html><body>fake</body></html>\n")

    # completions/* — excluded
    (out / "completions").mkdir(parents=True, exist_ok=True)
    (out / "completions/openclaw.bash").write_text("# fake completion\n" * 50)

    # credentials/*
    _write_json(out / "credentials/github-copilot.token.json", {
        "token": FAKE_GH_TOKEN, "expiresAt": "2026-12-31T00:00:00Z"
    })
    _write_json(out / "credentials/slack-pairing.json", {"pending": []})
    _write_json(out / "credentials/slack-default-allowFrom.json", {"allowFrom": ["U12345"]})

    # devices/*
    _write_json(out / "devices/paired.json", {
        "deviceA": {"tokens": {"operator": {"token": FAKE_OPERATOR_TOKEN}}}
    })
    _write_json(out / "devices/pending.json", {"requests": []})

    # identity/*
    _write_json(out / "identity/device.json", {
        "id": "device-FAKE-uuid",
        "privateKey": FAKE_ED25519_PRIV,
        "publicKey": FAKE_ED25519_PUB_B64,
    })
    _write_json(out / "identity/device-auth.json", {"bearer": FAKE_OPERATOR_TOKEN})

    # logs/* — excluded
    (out / "logs").mkdir(parents=True, exist_ok=True)
    (out / "logs/commands.log").write_text("ephemeral log\n")

    # SQLite DBs (memory, tasks, flows). Each leaves -wal/-shm siblings.
    _make_sqlite(
        out / "memory/main.sqlite",
        "CREATE TABLE t (id INTEGER PRIMARY KEY, content TEXT)",
        [(1, "alpha"), (2, "beta"), (3, "gamma")],
    )
    _make_sqlite(
        out / "tasks/runs.sqlite",
        "CREATE TABLE t (id INTEGER PRIMARY KEY, status TEXT)",
        [(1, "ok"), (2, "fail")],
    )
    _make_sqlite(
        out / "flows/registry.sqlite",
        "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)",
        [(1, "hello"), (2, "world")],
    )

    # qqbot/ plugin — excluded (in manifest exclude[])
    (out / "qqbot/data").mkdir(parents=True, exist_ok=True)
    (out / "qqbot/data/state.json").write_text('{"plugin":"qqbot"}\n')

    # telegram/* — data
    (out / "telegram").mkdir(parents=True, exist_ok=True)
    (out / "telegram/command-hash-default.txt").write_text("abc123def456\n")
    (out / "telegram/update-offset-default.json").write_text(json.dumps({"offset": 42}) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    build(args.out)
    print(f"fixture built: {args.out}")


if __name__ == "__main__":
    main()
