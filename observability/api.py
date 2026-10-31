"""Admin-only reporting routes; none invokes a billing or model tool."""
from contextlib import closing
import json
import sqlite3
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from database.connection import connect
from observability import analytics, pricing


class PricingRates(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model: str = Field(min_length=1, max_length=150)
    deployment: str = Field(default='', max_length=150)
    input_per_million: float = Field(ge=0, le=1000000, allow_inf_nan=False)
    output_per_million: float = Field(ge=0, le=1000000, allow_inf_nan=False)
    source_label: str = Field(default='Azure configured rates', min_length=1, max_length=120)

    @field_validator('input_per_million', 'output_per_million', mode='before')
    @classmethod
    def numeric_rates(cls, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError('Rates must be finite, nonnegative JSON numbers.')
        return value


def _response(data):
    return JSONResponse(data, headers={'Cache-Control':'no-store'})


def _code(value, maximum=100):
    if (isinstance(value, str) and len(value) <= maximum
            and all(char.isascii() and (char.isalnum() or char in '_-.') for char in value)):
        return value
    return None


def billing_audit(limit=100, offset=0):
    """Expose committed event metadata, never the raw contact/address arguments."""
    try:
        with closing(connect(readonly=True)) as conn:
            conn.execute('PRAGMA busy_timeout=250')
            conn.execute('BEGIN')
            total = conn.execute('SELECT COUNT(*) FROM billing_audit').fetchone()[0]
            records = conn.execute('SELECT id,action,created_at,details_json FROM billing_audit '
                                   'ORDER BY id DESC LIMIT ? OFFSET ?', (limit, offset)).fetchall()
        rows = []
        for record in records:
            try:
                arguments = json.loads(record['details_json'])
            except (ValueError, TypeError):
                arguments = {}
            arguments = arguments if isinstance(arguments, dict) else {}
            row = {'id':record['id'], 'created_at':record['created_at'],
                   'action':_code(record['action']), 'record_status':'committed'}
            for key in ('order_id', 'customer_id', 'invoice_number', 'address_id', 'target_order_id'):
                value = _code(arguments.get(key))
                if value:
                    row[key] = value
            rows.append(row)
        return {'available':True, 'rows':rows, 'total':total, 'limit':limit, 'offset':offset,
                'timezone':'UTC'}
    except (OSError, sqlite3.Error):
        return {'available':False, 'rows':[], 'total':0, 'limit':limit, 'offset':offset,
                'error':'The committed billing audit is temporarily unavailable.'}


def create_router(authorize_admin):
    async def access(x_admin_key: str | None = Header(default=None)):
        try:
            authorize_admin(x_admin_key)
        except HTTPException as exc:
            raise HTTPException(exc.status_code, exc.detail,
                                headers={'Cache-Control':'no-store'}) from exc

    router = APIRouter(prefix='/admin/observability', dependencies=[Depends(access)])

    @router.get('/summary')
    def summary(days: int = Query(default=7, ge=1, le=90)):
        return _response(analytics.summary(days=days))

    @router.get('/requests')
    def requests(days: int = Query(default=7, ge=1, le=90),
                 limit: int = Query(default=100, ge=1, le=200),
                 offset: int = Query(default=0, ge=0, le=1000000000),
                 mode: str | None = None, status: str | None = None):
        try:
            return _response(analytics.requests(days=days, limit=limit, offset=offset,
                                                mode=mode, status=status))
        except ValueError as exc:
            raise HTTPException(422, str(exc), headers={'Cache-Control':'no-store'}) from exc

    @router.get('/audit')
    def audit(limit: int = Query(default=100, ge=1, le=200),
              offset: int = Query(default=0, ge=0, le=1000000000)):
        return _response(billing_audit(limit, offset))

    @router.get('/pricing')
    def rates():
        return _response(pricing.get_pricing())

    @router.put('/pricing')
    def update_rates(body: PricingRates):
        try:
            return _response(pricing.save_pricing(**body.model_dump()))
        except ValueError as exc:
            raise HTTPException(422, str(exc), headers={'Cache-Control':'no-store'}) from exc
        except (OSError, sqlite3.Error) as exc:
            raise HTTPException(503, 'Pricing settings could not be saved. Billing can continue independently.',
                                headers={'Cache-Control':'no-store'}) from exc

    return router
