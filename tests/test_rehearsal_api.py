"""拆并预演 API：幂等裁定、修订局部重算、正式应用事务原子性、失败回滚。"""
import pytest
from fastapi.testclient import TestClient

from app import main, store
from app.rehearsal import store as rstore

ACK_SPLIT = '拆层改变上级申报来源，需办理变更申报'


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'test.sqlite'))
    monkeypatch.setattr(store, 'DB_PATH', str(tmp_path / 'test.sqlite'))
    return TestClient(main.app)


def _run(client, dataset='clean'):
    return client.post('/api/runs', json={'dataset': dataset}).json()


def _rehearse(client, run_id, payload=None):
    body = {'run_id': run_id}
    if payload is not None:
        body.update(payload)
    return client.post('/api/rehearsals', json=body)


def test_rehearsal_page_and_default_verdict(client):
    run = _run(client)
    response = _rehearse(client, run['manifest']['run_id'])
    assert response.status_code == 200
    data = response.json()
    assert data['summary']['total'] == 6
    assert {v['verdict'] for v in data['verdicts']} <= {'READY', 'SUPPLEMENT', 'BLOCKED'}
    page = client.get('/rehearsal?run_id=' + run['manifest']['run_id'])
    assert page.status_code == 200
    assert '拆并预演裁定' in page.text
    assert run['manifest']['run_id'] in page.text


def test_same_instruction_retry_does_not_create_second(client):
    run = _run(client)
    rid = run['manifest']['run_id']
    first = _rehearse(client, rid).json()
    second = _rehearse(client, rid).json()
    assert first['rehearsal_id'] == second['rehearsal_id']
    assert first['revision'] == 1
    history = client.get('/api/rehearsals', params={'run_id': rid}).json()
    assert len(history) == 1


def test_revision_only_recomputes_affected_masters(client):
    run = _run(client)
    rid = run['manifest']['run_id']
    first = _rehearse(client, rid).json()
    items = [dict(i) for i in first['instruction']['items'] if i['master_id'] != 'M005']
    items.append({'master_id': 'M005', 'plan': 'split'})
    revised = _rehearse(client, rid, {'items': items}).json()
    assert revised['revision'] == 2
    assert revised['rehearsal_id'] != first['rehearsal_id']
    reused = {v['master_id'] for v in revised['verdicts'] if v['reused']}
    assert reused == {'M001', 'M002', 'M003', 'M004', 'M006'}
    changed = next(v for v in revised['verdicts'] if v['master_id'] == 'M005')
    assert changed['reused'] is False
    # 两份裁定都保留，原申报依据不丢
    assert len(client.get('/api/rehearsals', params={'run_id': rid}).json()) == 2


def test_bad_plan_and_missing_run(client):
    run = _run(client)
    bad = _rehearse(client, run['manifest']['run_id'],
                    {'items': [{'master_id': 'M001', 'plan': 'nope'}]})
    assert bad.status_code == 422
    assert bad.json()['code'] == 'BAD_PLAN'
    missing = client.post('/api/rehearsals', json={'run_id': 'not-there'})
    assert missing.status_code == 404


def test_apply_requires_acknowledgement_and_preserves_source(client):
    run = _run(client)
    rehearsal = _rehearse(client, run['manifest']['run_id']).json()
    # 含阻断主单 M005：整票应用被拒
    blocked = client.post(f"/api/rehearsals/{rehearsal['rehearsal_id']}/apply",
                          json={'revision': 1, 'acknowledged_supplements': [ACK_SPLIT]})
    assert blocked.status_code == 422
    assert blocked.json()['code'] == 'VERDICT_BLOCKED'
    # 仅应用可执行的 M001（拆层，需补证确认）
    ok = client.post(f"/api/rehearsals/{rehearsal['rehearsal_id']}/apply",
                     json={'revision': 1, 'master_ids': ['M001'],
                           'acknowledged_supplements': [ACK_SPLIT]})
    assert ok.status_code == 200
    target_id = ok.json()['target_run_id']
    target = client.get('/api/runs/' + target_id).json()
    applied = [r for r in target['records'] if r.get('_applied')]
    assert applied and all(r['container'].startswith('C001-S') for r in applied)
    assert target['manifest']['derived_from'] == run['manifest']['run_id']
    # 旧版关系保持不变
    source = client.get('/api/runs/' + run['manifest']['run_id']).json()
    assert next(r for r in source['records'] if r['id'] == 'M001-P1')['container'] == 'C001'
    # 重复应用拒绝
    again = client.post(f"/api/rehearsals/{rehearsal['rehearsal_id']}/apply",
                        json={'revision': 1, 'master_ids': ['M001'],
                              'acknowledged_supplements': [ACK_SPLIT]})
    assert again.status_code == 409 and again.json()['code'] == 'ALREADY_APPLIED'


def test_apply_without_supplement_ack_is_rejected(client):
    run = _run(client)
    items = [{'master_id': 'M002', 'plan': 'split'},
             {'master_id': 'M006', 'plan': 'hold', 'capacity_pieces': 40, 'gross_kg': '450'}]
    rehearsal = _rehearse(client, run['manifest']['run_id'], {'items': items}).json()
    response = client.post(f"/api/rehearsals/{rehearsal['rehearsal_id']}/apply",
                           json={'revision': 1})
    assert response.status_code == 422
    assert response.json()['code'] == 'SUPPLEMENT_PENDING'


def test_stale_revision_conflict(client):
    run = _run(client)
    rehearsal = _rehearse(client, run['manifest']['run_id']).json()
    stale = client.post(f"/api/rehearsals/{rehearsal['rehearsal_id']}/apply",
                        json={'revision': rehearsal['revision'] + 1, 'master_ids': ['M006']})
    assert stale.status_code == 409
    assert stale.json()['code'] == 'STALE_VERSION'


def test_failed_apply_rolls_back_without_half_state(client):
    run = _run(client)
    items = [{'master_id': 'M006', 'plan': 'hold', 'capacity_pieces': 40, 'gross_kg': '450'}]
    rehearsal = _rehearse(client, run['manifest']['run_id'], {'items': items}).json()
    runs_before = len(client.get('/api/runs').json())

    def probe(con):
        raise RuntimeError('boom after writes')

    with pytest.raises(RuntimeError):
        rstore.apply_rehearsal(store.DB_PATH, store.get(store.DB_PATH, run['manifest']['run_id']),
                               rstore.get(store.DB_PATH, rehearsal['rehearsal_id']),
                               {'revision': rehearsal['revision']}, failure_probe=probe)
    # 新批次未落地、应用记录未写入；预演仍可再次应用
    assert len(client.get('/api/runs').json()) == runs_before
    retry = client.post(f"/api/rehearsals/{rehearsal['rehearsal_id']}/apply",
                        json={'revision': 1})
    assert retry.status_code == 200
