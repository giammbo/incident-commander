from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.models import effective_role
from app.services.markdown import render_markdown

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Available in every template (e.g. base.html nav gating).
templates.env.globals["effective_role"] = effective_role

# Jinja2 filter: {{ text | markdown }} → sanitized HTML string
templates.env.filters["markdown"] = render_markdown


def _from_json(s):
    import json

    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return []


templates.env.filters["from_json"] = _from_json

from app.services.custom_fields import display_value  # noqa: E402

templates.env.globals["display_value"] = display_value
