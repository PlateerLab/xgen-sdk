import pytest
from xgen_sdk.approval import blocks, evaluation, fields, store
from tests.test_approval_evaluation_options import EvalDB

DEFINITIONS = [dict(id='purpose', type='text', label='평가 목적', required=True, config={'max_length': 20}), dict(id='note', type='text', label='추가 의견', required=False, config={})]
OPTIONS = dict(mitigation=False, impact_scope=False, traits=False, attachments=False, agent_risk_grade=False, custom_fields=DEFINITIONS, sections=dict(scores=False, grades=False, rationale=False, extras=False))

def test_text_only_evaluation_and_requiredness():
    data = dict(options=OPTIONS, custom_values={'purpose': '검토\n결과'})
    assert blocks.evaluation_gaps(data) == []
    blocks.validate_data({'block_type': 'evaluation'}, data)
    data['custom_values'] = {}
    assert blocks.evaluation_gaps(data) == ['평가 목적']

@pytest.mark.parametrize('value', [True, 1, [], {}, 'a' * 21])
def test_invalid_text_rejected(value):
    with pytest.raises(ValueError):
        blocks.validate_data({'block_type': 'evaluation'}, dict(options=OPTIONS, custom_values={'purpose': value}))

def test_versioned_sections_and_inputs_cannot_be_bypassed():
    db = EvalDB(); db.add_template('custom', is_default=True)
    old = db.add_policy('custom', 1, OPTIONS, is_active=False)
    db.add_policy('custom', 2, dict(OPTIONS, custom_fields=[]))
    data = store._with_server_options(db, {'block_type': 'evaluation'}, {'policy_id': old, 'options': {'custom_fields': []}, 'custom_values': {}})
    assert data['options']['custom_fields'] == DEFINITIONS
    assert blocks.evaluation_gaps(data) == ['평가 목적']

@pytest.mark.parametrize('defs', [[dict(DEFINITIONS[0], type='unsupported')], DEFINITIONS + [DEFINITIONS[0]], [dict(DEFINITIONS[0], label='')], [dict(DEFINITIONS[0], config={'max_length': 0})]])
def test_invalid_definition(defs):
    with pytest.raises(ValueError): evaluation.normalize_options({'options': dict(OPTIONS, custom_fields=defs)})

def test_legacy_still_requires_scores_rationale_grade_and_scope():
    assert blocks.evaluation_gaps({}) == ['항목 점수', '판단 근거', '등급', '영향 범위']

def test_grade_requires_scoring():
    with pytest.raises(ValueError): evaluation.normalize_options({'options': dict(OPTIONS, sections=dict(scores=False, grades=True, rationale=False, extras=False))})
