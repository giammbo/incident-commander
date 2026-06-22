from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from app.auth import require_user
from app.db import get_db
from app.models import User
from app.services.service_map import build_graph
from app.templating import templates

router = APIRouter()


@router.get("/maps", response_class=HTMLResponse)
def maps_page(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "maps.html", {"current_user": user})


@router.get("/maps/graph.json")
def maps_graph(user: User = Depends(require_user), db: Session = Depends(get_db)):
    return JSONResponse(build_graph(db))
