from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.models import effective_role
from app.services.markdown import render_markdown

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Available in every template (e.g. base.html nav gating).
templates.env.globals["effective_role"] = effective_role

# Jinja2 filter: {{ text | markdown }} → sanitized HTML string
templates.env.filters["markdown"] = render_markdown
