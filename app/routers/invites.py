from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.invites import accept_invite, valid_invite
from app.templating import templates

router = APIRouter()


@router.get("/invite/accept", response_class=HTMLResponse)
def accept_page(request: Request, token: str = "", db: Session = Depends(get_db)):
    try:
        valid_invite(db, token)
        valid, error = True, None
    except ValueError as exc:
        valid, error = False, str(exc)
    return templates.TemplateResponse(
        request, "invite_accept.html", {"token": token, "valid": valid, "error": error}
    )


@router.post("/invite/accept")
def accept_submit(
    request: Request,
    token: str = Form(...),
    new_password: str = Form(...),
    confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        valid_invite(db, token)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "invite_accept.html",
            {"token": "", "valid": False, "error": str(exc)},
            status_code=400,
        )
    if new_password != confirm or len(new_password) < 8:
        return templates.TemplateResponse(
            request,
            "invite_accept.html",
            {
                "token": token,
                "valid": True,
                "error": "Passwords must match and be at least 8 characters.",
            },
            status_code=400,
        )
    try:
        accept_invite(db, token=token, new_password=new_password)
        db.commit()
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "invite_accept.html",
            {"token": token, "valid": False, "error": str(exc)},
            status_code=400,
        )
    request.session["flash"] = "Your account is ready — sign in."
    return RedirectResponse("/login", status_code=303)
