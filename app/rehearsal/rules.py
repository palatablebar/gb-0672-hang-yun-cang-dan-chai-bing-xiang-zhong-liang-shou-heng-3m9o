"""拆并预演裁定规则。

只读当前正式舱单快照（state 由不可变批次提供），不修改任何正式记录。
逐张主单对 hold（整票不动）/ split（按箱容拆到子层）两种方案做裁定：
- 件数/净重/毛重逐层守恒是否成立；
- 箱容与箱内毛重上限是否同时成立；
- 需要补证的位置；
- 能否进入正式执行。
"""
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal
import hashlib
import json

from app.errors import BusinessError

PLAN_HOLD = 'hold'
PLAN_SPLIT = 'split'
PLANS = (PLAN_HOLD, PLAN_SPLIT)
PLAN_LABELS = {PLAN_HOLD: '整票不动', PLAN_SPLIT: '按箱容拆层'}

DEFAULT_LIMITS = {'capacity_pieces': 30, 'gross_kg': '400'}
# 默认演示指令：覆盖 阻断/补证/可执行 三类裁定。
DEFAULT_ITEMS = [
    {'master_id': 'M001', 'plan': PLAN_SPLIT},
    {'master_id': 'M002', 'plan': PLAN_SPLIT},
    {'master_id': 'M003', 'plan': PLAN_SPLIT},
    {'master_id': 'M004', 'plan': PLAN_SPLIT},
    {'master_id': 'M005', 'plan': PLAN_HOLD},
    # 该票给出更宽的单票箱容，整票不动即可同时满足容量与重量。
    {'master_id': 'M006', 'plan': PLAN_HOLD, 'capacity_pieces': 40, 'gross_kg': '450'},
]

VERDICT_READY = 'READY'            # 可进入正式执行
VERDICT_SUPPLEMENT = 'SUPPLEMENT'  # 补证后可执行
VERDICT_BLOCKED = 'BLOCKED'        # 阻断，不能执行

FINDING_LABELS = {
    'MASTER_NOT_FOUND': '主单不存在于当前舱单',
    'GRAPH_ERROR': '拆并关系存在图错误',
    'UNKNOWN_HAZARD': '存在未登记危险类别',
    'INCOMPATIBLE_HAZARD': '同一分组内含不相容危险品',
    'WEIGHT_MISMATCH': '件数/净毛重守恒不成立',
    'OVER_CAPACITY': '箱内容量超限',
    'OVER_GROSS': '箱内毛重超限',
    'MISSING_UN': '危险品缺 UN 编号，需补证',
    'SPLIT_DECLARATION': '拆层改变上级申报来源，需办理变更申报',
    'SPLIT_NOT_NEEDED': '子层合计未超箱容，拆层不产生新分组',
}


def _positive_int(value, field):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise BusinessError('BAD_LIMIT', f'{field} 须为正整数')
    if number <= 0 or str(number) != str(value).strip():
        raise BusinessError('BAD_LIMIT', f'{field} 须为正整数')
    return number


def _positive_decimal(value, field):
    try:
        number = Decimal(str(value))
    except Exception:
        raise BusinessError('BAD_LIMIT', f'{field} 须为正数')
    if not number.is_finite() or number <= 0:
        raise BusinessError('BAD_LIMIT', f'{field} 须为正数')
    return format(number.normalize(), 'f')


def normalize_instruction(payload=None):
    """归一化并校验拆并箱指令，返回 {limits, items} 稳定结构。"""
    payload = payload or {}
    root = payload.get('instruction', payload)
    if not isinstance(root, dict):
        raise BusinessError('BAD_INSTRUCTION', '指令须为对象')
    raw_limits = root.get('limits', {}) or {}
    if not isinstance(raw_limits, dict):
        raise BusinessError('BAD_INSTRUCTION', 'limits 须为对象')
    limits = {
        'capacity_pieces': _positive_int(raw_limits.get('capacity_pieces', DEFAULT_LIMITS['capacity_pieces']), 'capacity_pieces'),
        'gross_kg': _positive_decimal(raw_limits.get('gross_kg', DEFAULT_LIMITS['gross_kg']), 'gross_kg'),
    }
    raw_items = root.get('items')
    if raw_items is None:
        raw_items = deepcopy(DEFAULT_ITEMS)
    if not isinstance(raw_items, list) or not raw_items:
        raise BusinessError('BAD_INSTRUCTION', 'items 须为非空数组')
    items = []
    seen = set()
    for raw in raw_items:
        if not isinstance(raw, dict) or not str(raw.get('master_id', '')).strip():
            raise BusinessError('BAD_INSTRUCTION', '每条指令须含 master_id')
        master_id = str(raw['master_id']).strip()
        plan = str(raw.get('plan', '')).strip()
        if plan not in PLANS:
            raise BusinessError('BAD_PLAN', f'{master_id} 方案无效，仅支持 hold/split')
        if master_id in seen:
            raise BusinessError('DUPLICATE_MASTER', f'{master_id} 在同一指令中重复')
        seen.add(master_id)
        item = {'master_id': master_id, 'plan': plan,
                'capacity_pieces': limits['capacity_pieces'], 'gross_kg': limits['gross_kg']}
        if 'capacity_pieces' in raw:
            item['capacity_pieces'] = _positive_int(raw['capacity_pieces'], 'capacity_pieces')
        if 'gross_kg' in raw:
            item['gross_kg'] = _positive_decimal(raw['gross_kg'], 'gross_kg')
        items.append(item)
    return {'limits': limits, 'items': items}


def instruction_fingerprint(instruction):
    body = json.dumps(instruction, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(body.encode()).hexdigest()


def _subtree(node_id, children):
    """返回子树全部节点（含自身），visited 防止依赖环造成死循环。"""
    out, stack, visited = [], [node_id], set()
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        out.append(current)
        stack.extend(children.get(current, []))
    return out


def _finding(code, severity, detail):
    return {'code': code, 'severity': severity, 'detail': detail, 'label': FINDING_LABELS.get(code, code)}


def _conservation(nodes, children, subtree_ids):
    """对子树内每个有子节点的节点做件数/净重/毛重守恒核验。"""
    checks, ok = [], True
    for parent in sorted(subtree_ids):
        kids = children.get(parent, [])
        if not kids:
            continue
        for field in ('pieces', 'net_kg', 'gross_kg'):
            declared = Decimal(str(nodes[parent][field]))
            total = sum((Decimal(str(nodes[k][field])) for k in kids), Decimal(0))
            difference = declared - total
            balanced = difference == 0
            ok = ok and balanced
            checks.append({'id': parent, 'field': field, 'declared': str(declared),
                           'children_sum': str(total), 'difference': str(difference),
                           'code': 'BALANCED' if balanced else 'WEIGHT_MISMATCH'})
    return checks, ok


def _hazard_set(nodes, children, member_ids):
    categories = set()
    for member in member_ids:
        for node_id in _subtree(member, children):
            category = str(nodes[node_id].get('hazard', 'none')).strip()
            if category != 'none':
                categories.add(category)
    return categories


def _pack(nodes, child_ids, capacity_pieces, gross_limit):
    """首次适应递减装箱：按毛重降序把直接子单放入分组。"""
    ordered = sorted(child_ids, key=lambda cid: Decimal(str(nodes[cid]['gross_kg'])), reverse=True)
    bins = []
    for child_id in ordered:
        node = nodes[child_id]
        for bin_group in bins:
            if (bin_group['pieces'] + node['pieces'] <= capacity_pieces
                    and Decimal(bin_group['gross_kg']) + Decimal(str(node['gross_kg'])) <= Decimal(str(gross_limit))):
                bin_group['members'].append(child_id)
                bin_group['pieces'] += node['pieces']
                bin_group['net_kg'] = str(Decimal(bin_group['net_kg']) + Decimal(str(node['net_kg'])))
                bin_group['gross_kg'] = str(Decimal(bin_group['gross_kg']) + Decimal(str(node['gross_kg'])))
                break
        else:
            bins.append({'members': [child_id], 'pieces': node['pieces'],
                         'net_kg': str(node['net_kg']), 'gross_kg': str(node['gross_kg'])})
    for index, bin_group in enumerate(bins, 1):
        violations = []
        if bin_group['pieces'] > capacity_pieces:
            violations.append('OVER_CAPACITY')
        if Decimal(bin_group['gross_kg']) > Decimal(str(gross_limit)):
            violations.append('OVER_GROSS')
        bin_group['violations'] = violations
    return bins


def _verdict_fingerprint(master_id, item, nodes, subtree_ids):
    evidence = [{'id': nid, 'revision': nodes[nid].get('revision'),
                 'pieces': nodes[nid]['pieces'], 'net_kg': nodes[nid]['net_kg'],
                 'gross_kg': nodes[nid]['gross_kg'], 'container': nodes[nid].get('container'),
                 'hazard': nodes[nid].get('hazard'), 'un_code': nodes[nid].get('un_code')}
                for nid in sorted(subtree_ids)]
    body = json.dumps({'item': item, 'evidence': evidence}, ensure_ascii=False,
                      sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(body.encode()).hexdigest()


def _hold_group(nodes, child_ids, original, capacity_pieces, gross_limit):
    """整票不动：全部直接子单留在原箱同一分组，不允许重新分箱。"""
    pieces = sum(nodes[cid]['pieces'] for cid in child_ids)
    net = sum((Decimal(str(nodes[cid]['net_kg'])) for cid in child_ids), Decimal(0))
    gross = sum((Decimal(str(nodes[cid]['gross_kg'])) for cid in child_ids), Decimal(0))
    violations = []
    if pieces > capacity_pieces:
        violations.append('OVER_CAPACITY')
    if gross > Decimal(str(gross_limit)):
        violations.append('OVER_GROSS')
    return {'group': original['container'] or 'ORIGIN', 'members': child_ids,
            'pieces': pieces, 'net_kg': str(net), 'gross_kg': str(gross),
            'violations': violations}


def verdict_for(state, item):
    nodes = {r['id']: r for r in state['records']}
    children = state['graph']['children']
    master_id, plan = item['master_id'], item['plan']
    findings = []
    master = nodes.get(master_id)
    if master is None:
        findings.append(_finding('MASTER_NOT_FOUND', 'blocker', f'{master_id} 不在当前正式舱单内'))
        return _assemble(master_id, item, None, [], [], True, findings, None)

    subtree_ids = _subtree(master_id, children)
    direct_children = sorted(children.get(master_id, []))
    original = {'container': master.get('container', ''), 'pieces': master['pieces'],
                'net_kg': str(master['net_kg']), 'gross_kg': str(master['gross_kg']),
                'revision': master.get('revision')}

    # 关系图错误（缺父级/依赖环等）落在本子树即阻断。
    graph_errors = [e for e in state['graph']['errors']
                    if e.get('id') in subtree_ids or any(p in subtree_ids for p in e.get('path', []))]
    for error in graph_errors:
        findings.append(_finding('GRAPH_ERROR', 'blocker', f"{'/'.join(error.get('path', []))} {error['code']}"))

    # 守恒核验：件数、净重、毛重必须同时成立。
    checks, conservation_ok = _conservation(nodes, children, subtree_ids)
    if not conservation_ok:
        bad = [c for c in checks if c['code'] == 'WEIGHT_MISMATCH']
        findings.append(_finding('WEIGHT_MISMATCH', 'blocker',
                                 '；'.join(f"{c['id']} {c['field']} 差异 {c['difference']}" for c in bad)))

    # 危险品：未知类别阻断；缺 UN 编号需补证；不相容类别若落入同一分组则阻断。
    unknown_hazards, missing_un = [], []
    valid_categories = set(state['context'].get('hazards', []))
    for node_id in subtree_ids:
        category = str(nodes[node_id].get('hazard', 'none')).strip()
        if category == 'none':
            continue
        if category not in valid_categories:
            unknown_hazards.append(node_id)
        elif not str(nodes[node_id].get('un_code', '')).strip():
            missing_un.append(node_id)
    for node_id in unknown_hazards:
        findings.append(_finding('UNKNOWN_HAZARD', 'blocker', f'{node_id} 危险类别未登记'))
    for node_id in missing_un:
        findings.append(_finding('MISSING_UN', 'supplement', f'{node_id} 缺 UN 编号'))

    # 分组与箱容/重量同时成立核验。
    if plan == PLAN_HOLD:
        groups = [_hold_group(nodes, direct_children, original,
                              item['capacity_pieces'], item['gross_kg'])]
    else:
        packed = _pack(nodes, direct_children, item['capacity_pieces'], item['gross_kg'])
        groups = []
        for index, group in enumerate(packed, 1):
            group['group'] = f"{original['container'] or master_id}-S{index}"
            groups.append(group)
        if len(groups) <= 1:
            findings.append(_finding('SPLIT_NOT_NEEDED', 'info', '单层即可容纳，拆层不产生新分组'))
        else:
            findings.append(_finding('SPLIT_DECLARATION', 'supplement',
                                     f'拆为 {len(groups)} 个箱组，上级申报来源变更，须办理变更申报'))

    incompatible_pairs = [tuple(pair) for pair in state['context'].get('incompatible', [])]
    for group in groups:
        categories = _hazard_set(nodes, children, group['members'])
        for a, b in incompatible_pairs:
            if a in categories and b in categories:
                findings.append(_finding('INCOMPATIBLE_HAZARD', 'blocker',
                                         f"箱组 {group['group']} 同时含 {a}/{b} 类危险品"))
        if 'OVER_CAPACITY' in group['violations']:
            findings.append(_finding('OVER_CAPACITY', 'blocker',
                                     f"箱组 {group['group']} 件数 {group['pieces']} 超箱容 {item['capacity_pieces']}"))
        if 'OVER_GROSS' in group['violations']:
            findings.append(_finding('OVER_GROSS', 'blocker',
                                     f"箱组 {group['group']} 毛重 {group['gross_kg']}kg 超上限 {item['gross_kg']}kg"))

    fingerprint = _verdict_fingerprint(master_id, item, nodes, subtree_ids)
    return _assemble(master_id, item, original, checks, groups, conservation_ok, findings, fingerprint)


def _assemble(master_id, item, original, checks, groups, conservation_ok, findings, fingerprint):
    blockers = [f['label'] for f in findings if f['severity'] == 'blocker']
    supplements = [f['label'] for f in findings if f['severity'] == 'supplement']
    if blockers:
        verdict = VERDICT_BLOCKED
    elif supplements:
        verdict = VERDICT_SUPPLEMENT
    else:
        verdict = VERDICT_READY
    capacity_holds = all(not g['violations'] for g in groups) if groups else False
    return {
        'master_id': master_id,
        'plan': item['plan'],
        'plan_label': PLAN_LABELS[item['plan']],
        'limits': {'capacity_pieces': item['capacity_pieces'], 'gross_kg': item['gross_kg']},
        'original': original,
        'conservation': {'rows': checks, 'ok': conservation_ok},
        'groups': groups,
        'capacity_and_weight_hold': capacity_holds and conservation_ok,
        'findings': findings,
        'blockers': blockers,
        'supplements': supplements,
        'verdict': verdict,
        'verdict_label': {VERDICT_READY: '可进入正式执行', VERDICT_SUPPLEMENT: '补证后可执行',
                          VERDICT_BLOCKED: '阻断，不能执行'}[verdict],
        'can_apply': not blockers,
        'verdict_fingerprint': fingerprint,
        'reused': False,
    }


def rehearse(state, instruction, prior=None):
    """执行整份指令；prior 为上一版预演时，未变化主单沿用旧裁定，只重算受影响主单。"""
    prior_index = {(v['master_id'], v['plan']): v for v in (prior or {}).get('verdicts', [])}
    verdicts = []
    for item in instruction['items']:
        verdict = verdict_for(state, item)
        previous = prior_index.get((item['master_id'], item['plan']))
        if previous and previous.get('verdict_fingerprint') and \
                previous['verdict_fingerprint'] == verdict['verdict_fingerprint']:
            verdict = deepcopy(previous)
            verdict['reused'] = True
            verdict['reused_from'] = (prior or {}).get('rehearsal_id')
        verdicts.append(verdict)
    summary = {
        'total': len(verdicts),
        'ready': sum(v['verdict'] == VERDICT_READY for v in verdicts),
        'supplement': sum(v['verdict'] == VERDICT_SUPPLEMENT for v in verdicts),
        'blocked': sum(v['verdict'] == VERDICT_BLOCKED for v in verdicts),
        'can_apply': sum(v['can_apply'] for v in verdicts),
    }
    return verdicts, summary
