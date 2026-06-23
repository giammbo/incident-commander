from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.auth import require_user
from app.db import get_db
from app.models import User
from app.services.insights import compute_insights, window_since
from app.templating import templates

router = APIRouter()


@router.get("/insights", response_class=HTMLResponse)
def insights_page(
    request: Request,
    days: int = 30,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    data = compute_insights(db, since=window_since(days))
    return templates.TemplateResponse(
        request, "insights.html", {"current_user": user, "data": data, "days": days}
    )
