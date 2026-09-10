"""Turning exceptions into readable CLI errors."""

from __future__ import annotations

import click
from pydantic import ValidationError


def clean(err: Exception) -> click.ClickException:
    """Wrap an exception for the CLI, unwrapping pydantic's verbose envelope."""
    if isinstance(err, ValidationError):
        msgs = [e["msg"].removeprefix("Value error, ") for e in err.errors()]
        return click.ClickException("\n".join(msgs) if msgs else str(err))
    return click.ClickException(str(err))
