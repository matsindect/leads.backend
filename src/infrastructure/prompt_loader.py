"""Loads Jinja2 prompt templates from the prompts/ directory.

Templates are loaded once at startup and cached.  Business logic
calls ``render(name, **kwargs)`` to get a filled prompt string.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader


class PromptLoader:
    """Manages Jinja2 prompt templates."""

    def __init__(self, prompts_dir: Path | None = None) -> None:
        if prompts_dir is None:
            prompts_dir = Path(__file__).resolve().parent.parent / "prompts"
        self._env = Environment(
            loader=FileSystemLoader(str(prompts_dir)),
            autoescape=False,
            keep_trailing_newline=True,
        )

    def render(self, template_name: str, **kwargs: object) -> str:
        """Render a named template with the given variables."""
        template = self._env.get_template(template_name)
        return template.render(**kwargs)
