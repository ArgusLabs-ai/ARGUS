"""``argus cloud`` — talk to a team workspace (docs/DATA.md)."""

from __future__ import annotations

import typer

from argus.cloud_baselines import pull
from argus.cloud_upload import UploadError, upload


def cloud_upload(final: bool) -> None:
    """Upload this job's run cards; one line either way, exit 1 when it did not work."""
    try:
        count = upload(final=final)
    except UploadError as exc:
        typer.echo(f"argus: {exc}", err=True)
        raise typer.Exit(1) from None
    cards = f"{count} run card" + ("" if count == 1 else "s")
    typer.echo(f"argus: uploaded {cards}" + (" (final)" if final else ""))


def cloud_pull_baselines() -> None:
    """Write the workspace's learned baselines; one line either way, exit 1 when it failed."""
    try:
        got = pull()
    except UploadError as exc:
        typer.echo(f"argus: {exc}", err=True)
        raise typer.Exit(1) from None
    if not (got.written or got.learning or got.skipped):
        typer.echo("argus: no baselines yet (learning from main)")
        return
    agents = f"{got.written} agent" + ("" if got.written == 1 else "s")
    rest = [
        f"{n} {what}" for n, what in ((got.learning, "learning"), (got.skipped, "skipped")) if n
    ]
    typer.echo(f"argus: pulled baselines for {agents}" + (f" ({', '.join(rest)})" if rest else ""))
