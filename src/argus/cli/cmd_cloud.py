"""``argus cloud`` — talk to a team workspace (docs/DATA.md)."""

from __future__ import annotations

import typer

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
