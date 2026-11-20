"""拆并预演裁定工作台入口。

页面：GET /rehearsal?run_id=...
- POST /api/rehearsals            生成/复用预演（同指令幂等；修订只重算受影响主单）
- GET  /api/rehearsals            预演历史
- GET  /api/rehearsals/{id}       预演详情
- POST /api/rehearsals/{id}/apply 正式应用（事务原子，失败回滚）
"""
from fastapi import APIRouter, Body, Request
from fastapi.templating import Jinja2Templates

from app import store
from app.config import META, ROOT
from app.engine import evaluate
from app.errors import BusinessError
from app.rehearsal import store as rstore
from app.rehearsal.rules import normalize_instruction

router = APIRouter()
templates = Jinja2Templates(directory=str(ROOT / 'app/templates'))


def _load_run(request: Request):
    run_id = request.query_params.get('run_id')
    if run_id:
        return store.get(store.DB_PATH, run_id)
    return store.save(store.DB_PATH, evaluate({'dataset': request.query_params.get('dataset', 'dirty')}))


@router.get('/rehearsal')
def page(request: Request):
    run = _load_run(request)
    history = rstore.list_for_run(store.DB_PATH, run['manifest']['run_id'])
    if history:
        selected = rstore.get(store.DB_PATH, history[-1]['rehearsal_id'])
    else:
        # 首次进入：用内置演示指令生成一份裁定（仍只读当前舱单，不改正式记录）。
        selected = rstore.upsert(store.DB_PATH, run, normalize_instruction({}))
        history = rstore.list_for_run(store.DB_PATH, run['manifest']['run_id'])
    return templates.TemplateResponse(
        request=request, name='rehearsal.html',
        context={'meta': META, 'key': 'rehearsal', 'state': run, 'rehearsal': selected,
                 'rehearsal_history': history})


@router.post('/api/rehearsals')
def create_rehearsal(payload: dict = Body(default={})):
    run_id = payload.get('run_id')
    if not run_id:
        raise BusinessError('BAD_RUN', '须指定 run_id 作为只读舱单依据')
    run = store.get(store.DB_PATH, str(run_id))
    instruction = normalize_instruction(payload)
    return rstore.upsert(store.DB_PATH, run, instruction)


@router.get('/api/rehearsals')
def rehearsal_history(run_id: str | None = None):
    if run_id:
        return rstore.list_for_run(store.DB_PATH, run_id)
    return rstore.history(store.DB_PATH)


@router.get('/api/rehearsals/{rehearsal_id}')
def rehearsal_detail(rehearsal_id: str):
    return rstore.get(store.DB_PATH, rehearsal_id)


@router.post('/api/rehearsals/{rehearsal_id}/apply')
def apply_rehearsal(rehearsal_id: str, payload: dict = Body(default={})):
    rehearsal = rstore.get(store.DB_PATH, rehearsal_id)
    source_run = store.get(store.DB_PATH, rehearsal['run_id'])
    return rstore.apply_rehearsal(store.DB_PATH, source_run, rehearsal, payload or {})
