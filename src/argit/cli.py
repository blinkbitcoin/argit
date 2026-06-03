"""argit CLI entry point.

Click command group: setup, doctor, info, review, backup, restore.
Top-level error handler renders ArgitError as the two-line first-touch
message and exits with the appropriate code.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from . import __version__
from .errors import ArgitError
from .shared import EXIT_FIRST_TOUCH


@click.group()
@click.version_option(__version__, prog_name="argit")
@click.pass_context
def main(ctx: click.Context) -> None:
    """argit — per-user agent backup/restore."""
    ctx.ensure_object(dict)
    ctx.obj["repo_root"] = Path.cwd()


@main.command("setup")
@click.option("--yes", is_flag=True, help="Skip interactive confirmation for IT-key import and auto-accept manifest upgrades.")
@click.option("--agent-key", "agent_key", default=None, help="Operator GPG fingerprint (required when multi-key).")
@click.option("--it-recipient", "it_recipient", default=None,
              help="GPG fingerprint of the backup/escrow recipient added alongside the agent key (greenfield only). Defaults to the bundled IT-backup key. Ignored when secrets/.gpg-id already exists.")
@click.option("--agent-type", "agent_type", default="openclaw", show_default=True,
              help="Bundled agent manifest to install.")
@click.option("--no-upgrade-manifest", "no_upgrade_manifest", is_flag=True,
              help="Do not prompt for bundled manifest upgrades; drift is still reported.")
@click.option("--dry-run", is_flag=True, help="Print actions without executing.")
@click.pass_context
def setup_cmd(ctx: click.Context, yes: bool, agent_key: str | None, it_recipient: str | None,
              agent_type: str,
              no_upgrade_manifest: bool, dry_run: bool) -> None:
    """One-time bootstrapping inside an existing git-init'd repo."""
    from .setup import run_setup
    run_setup(ctx.obj["repo_root"], yes=yes, agent_key=agent_key,
              it_recipient=it_recipient,
              agent_type=agent_type,
              no_upgrade_manifest=no_upgrade_manifest, dry_run=dry_run)


@main.command("doctor")
@click.option("--dry-run", is_flag=True, help="No-op alias (doctor is always non-mutating).")
@click.pass_context
def doctor_cmd(ctx: click.Context, dry_run: bool) -> None:
    """Diagnostic-only status report."""
    from .doctor import run_doctor
    code = run_doctor(ctx.obj["repo_root"])
    sys.exit(code)


@main.command("info")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON instead of human-readable lines.")
@click.pass_context
def info_cmd(ctx: click.Context, as_json: bool) -> None:
    """Print argit's bundled-resource paths + metadata (install-agnostic)."""
    from .info import run_info
    code = run_info(as_json=as_json)
    sys.exit(code)


@main.command("review")
@click.option("--dry-run", is_flag=True, help="Print what would be written without writing the report file.")
@click.pass_context
def review_cmd(ctx: click.Context, dry_run: bool) -> None:
    """Emit a flat list of uncovered paths in source_root as markdown (read-only)."""
    from .review import run_review
    code = run_review(ctx.obj["repo_root"], dry_run=dry_run)
    sys.exit(code)


@main.command("backup")
@click.option("--commit", is_flag=True, help="Stage + commit (no push).")
@click.option("--push", is_flag=True, help="Implies --commit, then `git push`.")
@click.option("--strict", is_flag=True, help="Fail hard on unspecified files.")
@click.option("--dry-run", is_flag=True, help="Print actions without executing.")
@click.pass_context
def backup_cmd(ctx: click.Context, commit: bool, push: bool, strict: bool, dry_run: bool) -> None:
    """Sanitize secrets, snapshot SQLite/data/blobs, optionally commit/push."""
    from .backup import run_backup
    run_backup(ctx.obj["repo_root"], commit=commit, push=push, strict=strict, dry_run=dry_run)


@main.command("restore")
@click.option("--target", "target", default=None, help="Override source_root (for tests / scratch restores).")
@click.option("--overwrite", is_flag=True, help="rm -rf target before restore. DESTROYS unmanaged files.")
@click.option("--merge", is_flag=True, help="Overlay restored state onto existing target.")
@click.option("--yes", is_flag=True, help="Skip interactive confirmation prompts.")
@click.option("--force", is_flag=True, help="Skip the lifecycle running-check.")
@click.option("--skip-lifecycle", is_flag=True, help="Bypass detect_running/stop/start entirely.")
@click.option("--dry-run", is_flag=True, help="Print actions without executing.")
@click.pass_context
def restore_cmd(ctx: click.Context, target: str | None, overwrite: bool, merge: bool, yes: bool,
                force: bool, skip_lifecycle: bool, dry_run: bool) -> None:
    """Re-inject secrets, rehydrate, verify."""
    if overwrite and merge:
        raise click.UsageError("--overwrite and --merge are mutually exclusive")
    from .restore import run_restore
    code = run_restore(
        ctx.obj["repo_root"],
        target=target,
        overwrite=overwrite,
        merge=merge,
        yes=yes,
        force=force,
        skip_lifecycle=skip_lifecycle,
        dry_run=dry_run,
    )
    sys.exit(code)


_cli = main  # save the click group; we shadow `main` below with the wrapped entry.


def _entrypoint() -> None:
    """Wraps click invocation with the ArgitError → first-touch handler."""
    try:
        _cli(prog_name="argit", standalone_mode=False)
    except ArgitError as exc:
        click.echo(str(exc), err=True)
        code = getattr(exc, "exit_code", EXIT_FIRST_TOUCH)
        sys.exit(code)
    except click.UsageError as exc:
        exc.show()
        sys.exit(exc.exit_code)
    except click.exceptions.Abort:
        click.echo("Aborted.", err=True)
        sys.exit(1)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        click.echo("Interrupted.", err=True)
        sys.exit(130)


def main() -> None:  # type: ignore[no-redef]
    """Entry point referenced by `pyproject.toml` console-scripts."""
    _entrypoint()


if __name__ == "__main__":
    main()
