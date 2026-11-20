"""``argus cloud`` — talk to a team workspace (docs/DATA.md)."""

from __future__ import annotations

import typer

from argus.cloud_baselines import pull
from argus.cloud_upload import UploadError, upload


def cloud_upload(final: bool) -> None:
    """Upload this job's run cards; one line either way, exit 1 when it did not work."""
    try:
        got = upload(final=final)
    except UploadError as exc:
        typer.echo(f"argus: {exc}", err=True)
        raise typer.Exit(1) from None
    cards = f"{got.cards} run card" + ("" if got.cards == 1 else "s")
    extra = []
    if got.payloads:
        extra.append(f"{got.payloads} with full payload")
    if got.too_large:
        extra.append(
            f"{got.too_large} payload{'' if got.too_large == 1 else 's'} too large to send"
        )
    typer.echo(
        f"argus: uploaded {cards}"
        + (f" ({', '.join(extra)})" if extra else "")
        + (" (final)" if final else "")
    )


def cloud_pull_baselines() -> None:
    """Write the workspace's learned baselines; one line either way, exit 1 when it failed."""
    try:
        got = pull()
    except UploadError as exc:
        typer.echo(f"argus: {exc}", err=True)
        raise typer.Exit(1) from None
    if got.full_payloads:
        typer.echo("argus: this project sends full payloads (real values) with its run cards")
    if not (got.written or got.learning or got.skipped):
        typer.echo("argus: no baselines yet (learning from main)")
        return
    agents = f"{got.written} agent" + ("" if got.written == 1 else "s")
    rest = [
        f"{n} {what}" for n, what in ((got.learning, "learning"), (got.skipped, "skipped")) if n
    ]
    typer.echo(f"argus: pulled baselines for {agents}" + (f" ({', '.join(rest)})" if rest else ""))
