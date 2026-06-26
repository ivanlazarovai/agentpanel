"""The live permission prompt — shown when an executing agent needs a gated command.

Surfaces the request (tool, target, AgentPanel's risk judgement) and the three answers:
allow once, allow this type (remembered), or deny. Returned as a string the app acts on.
"""

from __future__ import annotations

from typing import Dict

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

_RISK_COLOR = {"low": "green", "medium": "yellow", "high": "red", "critical": "red"}


class ApprovalModal(ModalScreen[str]):
    """Returns 'allow_once' | 'allow_type' | 'deny'."""

    CSS = """
    ApprovalModal { align: center middle; }
    #box { width: 78; height: auto; border: thick $warning; background: $surface; padding: 1 2; }
    #box .req { margin: 1 0; }
    #box Horizontal { height: auto; align-horizontal: center; }
    #box Button { margin: 1 1 0 1; }
    """

    def __init__(self, req: Dict) -> None:
        super().__init__()
        self.req = req

    def compose(self) -> ComposeResult:
        r = self.req
        risk = str(r.get("risk", "?"))
        color = _RISK_COLOR.get(risk, "white")
        with Vertical(id="box"):
            yield Static("[bold]An agent needs your approval to run a gated command.[/]",
                         markup=True)
            yield Static(
                f"[bold]{r.get('tool', '?')}[/]  →  {str(r.get('target', '')) [:120]}\n"
                f"risk: [{color}]{risk}[/]   ·   {r.get('reason', '')}",
                classes="req", markup=True,
            )
            yield Static("[dim]'Allow this type' is remembered for future commands like this.[/]",
                         markup=True)
            with Horizontal():
                yield Button("Allow once", id="allow_once", variant="success")
                yield Button("Allow this type", id="allow_type", variant="primary")
                yield Button("Deny", id="deny", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id or "deny")
