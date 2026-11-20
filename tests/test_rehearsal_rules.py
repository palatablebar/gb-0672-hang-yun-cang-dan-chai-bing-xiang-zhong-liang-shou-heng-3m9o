"""拆并预演裁定：只读、逐票裁定、幂等、修订局部重算、正式应用与原子回滚。"""
from app.engine import evaluate
from app.rehearsal.rules import (
    normalize_instruction, rehearse, verdict_for,
    VERDICT_BLOCKED, VERDICT_SUPPLEMENT, VERDICT_READY,
)


def test_default_instruction_covers_three_verdicts(sample):
    verdicts, summary = rehearse(sample, normalize_instruction({}))
    by_id = {v['master_id']: v for v in verdicts}
    # M001 现场批毛重差异 1kg：守恒不成立，即使拆层仍阻断，并需补 UN/变更申报
    assert by_id['M001']['verdict'] == VERDICT_BLOCKED
    assert by_id['M001']['capacity_and_weight_hold'] is False
    # M002..M004 拆层后箱容与重量同时成立，但改变申报来源须补证
    for master in ('M002', 'M003', 'M004'):
        assert by_id[master]['verdict'] == VERDICT_SUPPLEMENT
        assert by_id[master]['capacity_and_weight_hold'] is True
        assert '拆层改变上级申报来源，需办理变更申报' in by_id[master]['supplements']
    # M005 整票不动：40 件/440kg 同时超箱容 30 与上限 400，阻断
    assert by_id['M005']['verdict'] == VERDICT_BLOCKED
    assert by_id['M005']['plan'] == 'hold'
    assert set(by_id['M005']['blockers']) >= {'箱内容量超限', '箱内毛重超限'}
    # M006 放宽单票箱容后整票不动即可同时成立，直接可执行
    assert by_id['M006']['verdict'] == VERDICT_READY
    assert by_id['M006']['can_apply'] is True
    assert summary == {'total': 6, 'ready': 1, 'supplement': 3, 'blocked': 2, 'can_apply': 4}


def test_rehearse_is_read_only(clean):
    import copy
    snapshot = copy.deepcopy(clean['records'])
    rehearse(clean, normalize_instruction({}))
    assert clean['records'] == snapshot
    assert clean['manifest'] == evaluate({'dataset': 'clean'})['manifest']


def test_capacity_and_weight_must_hold_together(clean):
    # 只超重量不超件数
    v = verdict_for(clean, {'master_id': 'M006', 'plan': 'hold',
                            'capacity_pieces': 40, 'gross_kg': '400'})
    assert v['verdict'] == VERDICT_BLOCKED
    assert '箱内毛重超限' in v['blockers']
    assert '箱内容量超限' not in v['blockers']
    # 只超件数不超重量
    v2 = verdict_for(clean, {'master_id': 'M006', 'plan': 'hold',
                             'capacity_pieces': 30, 'gross_kg': '500'})
    assert '箱内容量超限' in v2['blockers']
    assert '箱内毛重超限' not in v2['blockers']


def test_split_packs_within_limits(clean):
    v = verdict_for(clean, {'master_id': 'M006', 'plan': 'split',
                            'capacity_pieces': 30, 'gross_kg': '400'})
    assert len(v['groups']) == 2
    assert all(not g['violations'] for g in v['groups'])
    assert v['capacity_and_weight_hold'] is True


def test_unknown_master_blocked_not_raised(clean):
    v = verdict_for(clean, {'master_id': 'GHOST', 'plan': 'hold',
                            'capacity_pieces': 30, 'gross_kg': '400'})
    assert v['verdict'] == VERDICT_BLOCKED
    assert v['can_apply'] is False


def test_conservation_mismatch_blocks_split(sample):
    # 现场批 M001 毛重申报 441，子单合计 440
    v = verdict_for(sample, {'master_id': 'M001', 'plan': 'split',
                             'capacity_pieces': 30, 'gross_kg': '400'})
    assert v['conservation']['ok'] is False
    bad = [c for c in v['conservation']['rows'] if c['code'] == 'WEIGHT_MISMATCH']
    assert any(c['field'] == 'gross_kg' and c['difference'] == '1' for c in bad)
    assert v['verdict'] == VERDICT_BLOCKED


def test_instruction_validation():
    from app.errors import BusinessError
    for payload, code in [
        ({'items': [{'master_id': 'M001', 'plan': 'merge'}]}, 'BAD_PLAN'),
        ({'limits': {'capacity_pieces': 0}}, 'BAD_LIMIT'),
        ({'limits': {'gross_kg': 'x'}}, 'BAD_LIMIT'),
        ({'items': []}, 'BAD_INSTRUCTION'),
        ({'items': [{'master_id': 'M001', 'plan': 'hold'},
                    {'master_id': 'M001', 'plan': 'split'}]}, 'DUPLICATE_MASTER'),
    ]:
        try:
            normalize_instruction(payload)
            assert False, payload
        except BusinessError as error:
            assert error.code == code


def test_single_bin_split_is_info_not_supplement(clean):
    v = verdict_for(clean, {'master_id': 'M006', 'plan': 'split',
                            'capacity_pieces': 40, 'gross_kg': '450'})
    codes = [f['code'] for f in v['findings']]
    assert 'SPLIT_NOT_NEEDED' in codes
    assert 'SPLIT_DECLARATION' not in codes
    assert v['verdict'] == VERDICT_READY


def test_incompatible_hazard_blocks_hold_but_split_separates(sample):
    hold = verdict_for(sample, {'master_id': 'M001', 'plan': 'hold',
                                'capacity_pieces': 40, 'gross_kg': '500'})
    hold_codes = {f['code'] for f in hold['findings']}
    assert 'INCOMPATIBLE_HAZARD' in hold_codes
    assert 'MISSING_UN' in hold_codes
    assert hold['verdict'] == VERDICT_BLOCKED
    split = verdict_for(sample, {'master_id': 'M001', 'plan': 'split',
                                 'capacity_pieces': 30, 'gross_kg': '400'})
    assert 'INCOMPATIBLE_HAZARD' not in {f['code'] for f in split['findings']}
