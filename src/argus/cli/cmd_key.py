"""argus key — manage local BYOK LLM API keys (OpenAI / Anthropic / Google)."""

from __future__ import annotations

from rich.console import Console

from argus import user_config
from argus.providers import PROVIDERS

_console = Console()

_ENV_HINT = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GEMINI_API_KEY",
}


def _mask(key: str) -> str:
    if len(key) <= 8:
        return "•" * len(key)
    return f"{key[:3]}…{key[-4:]}"


def _validate_provider(provider: str) -> bool:
    if provider not in PROVIDERS:
        _console.print(
            f"  [red]Unknown provider '{provider}'.[/red] "
            f"Choose from: {', '.join(PROVIDERS)}."
        )
        return False
    return True


def key_set(value: str | None = None, provider: str = "openai") -> None:
    """Save a provider key locally and make it the active provider."""
    if not _validate_provider(provider):
        return
    if not value:
        import getpass

        value = getpass.getpass(f"{provider} API key (input hidden): ").strip()
    if not value:
        _console.print("  [red]No key entered.[/red]")
        return
    user_config.set_key(provider, value)
    user_config.set_provider(provider)
    _console.print(
        f"  [green]Saved[/green] {provider} key {_mask(value)} to ~/.argus/config.json"
    )
    _console.print(
        f"  [dim]BYOK enabled — AI-powered detection now uses your {provider} key.\n"
        "  Switch providers anytime with [bold]argus key use <provider>[/bold].\n"
        "  Check status with [bold]argus doctor[/bold].[/dim]"
    )


def key_use(provider: str) -> None:
    """Switch the active provider (must already have a key)."""
    if not _validate_provider(provider):
        return
    if not user_config.resolve_key(provider):
        _console.print(
            f"  [yellow]No {provider} key set.[/yellow] Run "
            f"[bold]argus key set --provider {provider}[/bold] or export "
            f"{_ENV_HINT[provider]}."
        )
        return
    user_config.set_provider(provider)
    _console.print(f"  [green]Active provider set to[/green] {provider}")


def key_show() -> None:
    """List configured providers (masked) and mark the active one."""
    active = user_config.get_provider()
    any_shown = False
    for provider in PROVIDERS:
        key = user_config.resolve_key(provider)
        if not key:
            continue
        any_shown = True
        marker = "[bold green]* [/bold green]" if provider == active else "  "
        _console.print(f"  {marker}{provider}: {_mask(key)}")
    if not any_shown:
        _console.print(
            "  [yellow]No LLM key set.[/yellow] Run [bold]argus key set[/bold] "
            "(add [bold]--provider anthropic|google[/bold] for another provider)."
        )
    else:
        _console.print(f"  [dim]active provider: {active}[/dim]")


def key_clear(provider: str | None = None) -> None:
    """Remove one provider's key, or all saved keys when omitted."""
    if provider is not None and not _validate_provider(provider):
        return
    user_config.clear_key(provider)
    what = f"{provider} key" if provider else "all saved keys"
    _console.print(f"  [green]Cleared[/green] {what} from ~/.argus/config.json")
