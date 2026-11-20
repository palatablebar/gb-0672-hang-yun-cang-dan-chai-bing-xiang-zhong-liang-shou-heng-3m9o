"""拆并预演持久化与正式应用。

- rehearsals：每批舱单、每份指令指纹只保留一条预演（同一指令重试不新增第二套）。
- rehearsal_revisions：指令修订留痕，保留原申报依据。
- applications：正式应用记录；apply_rehearsal 在单事务内写新批次，任何失败回滚，
  旧版关系（原 runs 行）始终保留，绝不留半套。
"""
import json
import sqlite3
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone

from app.errors import BusinessError
from app.rehearsal.rules import (
    PLAN_SPLIT, VERDICT_BLOCKED, VERDICT_SUPPLEMENT,
    instruction_fingerprint, normalize_instruction, rehearse,
)

DDL = [
    """CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核'
    )""",
    """CREATE TABLE IF NOT EXISTS rehearsals (
        rehearsal_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        instruction_fingerprint TEXT NOT NULL,
        instruction TEXT NOT NULL,
        verdict TEXT NOT NULL,
        summary TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(run_id, instruction_fingerprint)
    )""",
    """CREATE TABLE IF NOT EXISTS rehearsal_revisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rehearsal_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        instruction TEXT NOT NULL,
        changed_masters TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS applications (
        application_id TEXT PRIMARY KEY,
        rehearsal_id TEXT NOT NULL,
        source_run_id TEXT NOT NULL,
        target_run_id TEXT NOT NULL,
        masters TEXT NOT NULL,
        acknowledged TEXT NOT NULL,
        created_at TEXT NOT NULL,
        failure TEXT
    )""",
]


@contextmanager
def connect(path):
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA foreign_keys=ON')
    try:
        for statement in DDL:
            con.execute(statement)
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _save_run_in_tx(con, result):
    """在当前事务连接内写新批次；与应用记录同生共死，保证不出现半套。"""
    run_id = result['manifest']['run_id']
    con.execute(
        'INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)',
        (run_id, result['manifest']['sha256'], json.dumps(result, ensure_ascii=False)))
    row = con.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
    loaded = json.loads(row['result'])
    loaded.update(revision=row['revision'], status=row['status'])
    return loaded


def _overall(verdicts):
    if any(v['verdict'] == VERDICT_BLOCKED for v in verdicts):
        return VERDICT_BLOCKED
    if any(v['verdict'] == VERDICT_SUPPLEMENT for v in verdicts):
        return VERDICT_SUPPLEMENT
    return 'READY'


def _changed_masters(old_instruction, new_instruction):
    old = {item['master_id']: item for item in (old_instruction or {}).get('items', [])}
    changed = []
    for item in new_instruction['items']:
        if old.get(item['master_id']) != item:
            changed.append(item['master_id'])
    return changed


def upsert(path, run, instruction, prior_rehearsal=None):
    """幂等保存预演；指令修订时只重算受影响主单，沿用未变化主单的旧裁定。"""
    fingerprint = instruction_fingerprint(instruction)
    with connect(path) as con:
        existing = con.execute(
            'SELECT * FROM rehearsals WHERE run_id=? AND instruction_fingerprint=?',
            (run['manifest']['run_id'], fingerprint)).fetchone()
        if existing:  # 同一指令重试：不新增第二套裁定，仅回显既有记录。
            return _load(con, existing['rehearsal_id'])

        prior = prior_rehearsal
        if prior is None:
            row = con.execute(
                'SELECT * FROM rehearsals WHERE run_id=? ORDER BY revision DESC LIMIT 1',
                (run['manifest']['run_id'],)).fetchone()
            if row:
                prior = json.loads(row['verdict'])
                prior['instruction'] = json.loads(row['instruction'])
                prior['rehearsal_id'] = row['rehearsal_id']

        verdicts, summary = rehearse(run, instruction, prior)
        rehearsal_id = f"RH-{run['manifest']['run_id']}-{fingerprint[:10]}"
        stamp = _now()
        revision = 1
        changed = [item['master_id'] for item in instruction['items']]
        if prior is not None:
            revision = con.execute(
                'SELECT COALESCE(MAX(revision),0)+1 FROM rehearsals WHERE run_id=?',
                (run['manifest']['run_id'],)).fetchone()[0]
            changed = _changed_masters(prior.get('instruction'), instruction)
        record = {
            'rehearsal_id': rehearsal_id, 'run_id': run['manifest']['run_id'],
            'instruction_fingerprint': fingerprint, 'instruction': instruction,
            'verdicts': verdicts, 'summary': summary, 'revision': revision,
            'source_manifest': run['manifest'], 'created_at': stamp,
            'overall': _overall(verdicts),
        }
        con.execute(
            'INSERT INTO rehearsals(rehearsal_id,run_id,instruction_fingerprint,instruction,verdict,summary,revision,created_at,updated_at)'
            ' VALUES(?,?,?,?,?,?,?,?,?)',
            (rehearsal_id, run['manifest']['run_id'], fingerprint,
             json.dumps(instruction, ensure_ascii=False), json.dumps(record, ensure_ascii=False),
             json.dumps(summary, ensure_ascii=False), revision, stamp, stamp))
        con.execute(
            'INSERT INTO rehearsal_revisions(rehearsal_id,revision,instruction,changed_masters,created_at)'
            ' VALUES(?,?,?,?,?)',
            (rehearsal_id, revision, json.dumps(instruction, ensure_ascii=False),
             json.dumps(changed, ensure_ascii=False), stamp))
        return record


def _load(con, rehearsal_id):
    row = con.execute('SELECT * FROM rehearsals WHERE rehearsal_id=?', (rehearsal_id,)).fetchone()
    if not row:
        raise BusinessError('REHEARSAL_NOT_FOUND', '预演不存在', 404)
    record = json.loads(row['verdict'])
    record['revision'] = row['revision']
    record['overall'] = _overall(record['verdicts'])
    return record


def get(path, rehearsal_id):
    with connect(path) as con:
        return _load(con, rehearsal_id)


def list_for_run(path, run_id):
    with connect(path) as con:
        rows = con.execute(
            'SELECT rehearsal_id,revision,summary,created_at,updated_at FROM rehearsals WHERE run_id=? ORDER BY revision',
            (run_id,)).fetchall()
        return [dict(r, summary=json.loads(r['summary'])) for r in rows]


def history(path):
    with connect(path) as con:
        rows = con.execute(
            'SELECT rehearsal_id,run_id,revision,summary,created_at FROM rehearsals ORDER BY rowid DESC LIMIT 100'
        ).fetchall()
        return [dict(r, summary=json.loads(r['summary'])) for r in rows]


def _build_target_run(run, verdicts, acknowledged):
    """依据可执行裁定派生新版本舱单状态；正式舱单只读，这里产出的是新批次。"""
    blocked = [v['master_id'] for v in verdicts if v['verdict'] == VERDICT_BLOCKED]
    if blocked:
        raise BusinessError('VERDICT_BLOCKED', f"以下主单阻断，不能正式应用：{','.join(blocked)}")
    required = {v['master_id']: set(v['supplements']) for v in verdicts if v['supplements']}
    ack = set(acknowledged or [])
    missing = {master: sorted(needed - ack) for master, needed in required.items() if needed - ack}
    if missing:
        raise BusinessError('SUPPLEMENT_PENDING', f'补证未确认：{json.dumps(missing, ensure_ascii=False)}')

    nodes = {r['id']: r for r in run['records']}
    children = run['graph']['children']
    new_records = deepcopy(run['records'])
    new_nodes = {r['id']: r for r in new_records}
    applied = []
    for verdict in verdicts:
        master_id = verdict['master_id']
        if verdict['plan'] == PLAN_SPLIT:
            for group in verdict['groups']:
                for child_root in group['members']:
                    for node_id in _walk(children, child_root):
                        new_nodes[node_id]['container'] = group['group']
                        new_nodes[node_id]['revision'] = int(new_nodes[node_id].get('revision', 1)) + 1
                        new_nodes[node_id]['_applied'] = f"split:{master_id}"
            applied.append({'master_id': master_id, 'plan': verdict['plan'],
                            'groups': [g['group'] for g in verdict['groups']]})
        else:
            applied.append({'master_id': master_id, 'plan': verdict['plan'],
                            'groups': [verdict['original']['container'] or master_id]})

    target = deepcopy(run)
    target['records'] = new_records
    target['source'] = run['source']
    target['application_source_run_id'] = run['manifest']['run_id']
    target['application_applied'] = applied
    target['application_acknowledged'] = sorted(ack)
    target['manifest'] = _derive_manifest(run['manifest'], applied, verdict_count=len(verdicts))
    target.setdefault('views', {})['application'] = {
        'title': '正式应用', 'rows': applied,
        'metrics': {'应用主单': len(applied), '补证确认': len(ack)}}
    return target, applied


def _walk(children, node_id):
    out, stack, seen = [], [node_id], set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        out.append(current)
        stack.extend(children.get(current, []))
    return out


def _derive_manifest(base_manifest, applied, verdict_count):
    stamp = datetime.now(timezone.utc)
    digest_seed = json.dumps(
        {'base': base_manifest['sha256'], 'applied': applied},
        ensure_ascii=False, sort_keys=True).encode()
    import hashlib
    digest = hashlib.sha256(digest_seed).hexdigest()
    return {
        'run_id': f"AP-{base_manifest['run_id']}-{digest[:8]}",
        'sha256': digest,
        'rule_version': base_manifest['rule_version'] + '+apply',
        'source': base_manifest['source'],
        'generated_at': stamp.isoformat(),
        'elapsed_ms': 0.0,
        'timezone': 'Asia/Shanghai',
        'derived_from': base_manifest['run_id'],
        'applied_masters': verdict_count,
    }


def apply_rehearsal(path, source_run, rehearsal, payload, failure_probe=None):
    """正式应用：单事务写新批次+应用记录；失败整体回滚，旧版关系保持不变。"""
    acknowledged = payload.get('acknowledged_supplements', [])
    master_filter = payload.get('master_ids')
    if acknowledged is not None and not isinstance(acknowledged, list):
        raise BusinessError('BAD_ACKNOWLEDGEMENT', 'acknowledged_supplements 须为数组')
    verdicts = rehearsal['verdicts']
    if master_filter is not None:
        if not isinstance(master_filter, list) or not master_filter:
            raise BusinessError('BAD_MASTER_FILTER', 'master_ids 须为非空数组')
        known = {v['master_id'] for v in verdicts}
        unknown = sorted(set(master_filter) - known)
        if unknown:
            raise BusinessError('MASTER_NOT_FOUND', f"裁定中无此主单：{','.join(unknown)}")
        verdicts = [v for v in verdicts if v['master_id'] in set(master_filter)]

    target_run, applied = _build_target_run(source_run, verdicts, acknowledged)
    application_id = f"APL-{rehearsal['rehearsal_id'][3:]}-{target_run['manifest']['run_id'][-8:]}"

    with connect(path) as con:
        # 乐观锁：预演被修订后旧 revision 不能再应用。
        current = con.execute('SELECT revision FROM rehearsals WHERE rehearsal_id=?',
                              (rehearsal['rehearsal_id'],)).fetchone()
        expected = payload.get('revision')
        if expected is not None and int(expected) != current['revision']:
            raise BusinessError('STALE_VERSION', '预演已修订，请按最新版本重新裁定', 409)
        duplicate = con.execute('SELECT 1 FROM applications WHERE rehearsal_id=?',
                                (rehearsal['rehearsal_id'],)).fetchone()
        if duplicate:
            raise BusinessError('ALREADY_APPLIED', '该预演已正式应用，不能重复执行', 409)
        target_run = _save_run_in_tx(con, target_run)  # 与应用记录在同一事务内提交
        con.execute(
            'INSERT INTO applications(application_id,rehearsal_id,source_run_id,target_run_id,masters,acknowledged,created_at)'
            ' VALUES(?,?,?,?,?,?,?)',
            (application_id, rehearsal['rehearsal_id'], source_run['manifest']['run_id'],
             target_run['manifest']['run_id'], json.dumps(applied, ensure_ascii=False),
             json.dumps(sorted(acknowledged), ensure_ascii=False), _now()))
        if failure_probe is not None:
            failure_probe(con)  # 测试钩子：抛错即整体回滚
    return {'application_id': application_id, 'rehearsal_id': rehearsal['rehearsal_id'],
            'source_run_id': source_run['manifest']['run_id'],
            'target_run_id': target_run['manifest']['run_id'],
            'applied': applied, 'acknowledged': sorted(acknowledged),
            'rolled_back': False}
