from fastapi import APIRouter, Depends, Query, Response

from src.constants.enums import Role
from src.controllers.views import correction_view, ledger_cell_view
from src.models.base import Session
from src.services.auth import current_user, roles
from src.services.corrections import ledger_rows, list_corrections, submit_correction

router = APIRouter(prefix='/api/corrections')
editor = roles(Role.ANALYST, Role.SUPERVISOR)


@router.get('')
def index(sheet: str | None = Query(None),
          offset: int = Query(0, ge=0),
          limit: int = Query(20, ge=1, le=100),
          user=Depends(current_user)):
    with Session() as db:
        total, items = list_corrections(db, sheet=sheet.strip() if sheet else None,
                                        offset=offset, limit=limit)
        return {'total': total, 'offset': offset,
                'items': [correction_view(item) for item in items]}


@router.post('')
def submit(payload: dict, response: Response, user=Depends(editor)):
    with Session.begin() as db:
        item, created = submit_correction(db, payload, user['sub'])
        response.status_code = 201 if created else 200
        return correction_view(item)


@router.get('/ledger')
def ledger(user=Depends(current_user)):
    with Session() as db:
        return {'items': [ledger_cell_view(row) for row in ledger_rows(db)]}
