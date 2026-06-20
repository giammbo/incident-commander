from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import require_user
from app.db import get_db
from app.models import User
from app.security.passwords import hash_password
from app.templating import templates

router = APIRouter()


@router.get("/account/password", response_class=HTMLResponse)
def password_page(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(
        request, "account_password.html", {"current_user": user, "error": None}
    )


@router.post("/account/password")
def password_submit(
    request: Request,
    new_password: str = Form(...),
    confirm: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if new_password != confirm or len(new_password) < 8:
        return templates.TemplateResponse(
            request,
            "account_password.html",
            {"current_user": user, "error": "Passwords must match and be at least 8 chars"},
            status_code=400,
        )
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    db.add(user)
    db.commit()
    return RedirectResponse("/", status_code=303)
