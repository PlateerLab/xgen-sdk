import copy
import math
import pytest
from xgen_sdk.approval.fields import evaluation_fields, evaluation_value_errors
from xgen_sdk.approval.formula import computed_values
from xgen_sdk.approval import store
from tests.test_approval_evaluation_options import EvalDB


def field(id, type, **config):
    return dict(id=id, type=type, label=id, required=True, config=config)


FIELDS = [field('a', 'number'), field('b', 'number'), field('day', 'date'),
          field('choice', 'select', choices=[dict(code='yes', label='예')]),
          field('check', 'checkbox'), field('sum', 'formula', expression='([a] + [b]) * 2'),
          field('chain', 'formula', expression='[sum] / 2 + -1.5')]
VALUES = dict(a=2.5, b=3, day='2024-02-29', choice='yes', check=False, sum=999, chain=-999)


def test_section_order_is_preserved_and_missing_sections_are_appended():
    from xgen_sdk.approval.evaluation import normalize_options
    raw = {'options': {'section_order': ['rationale', 'data']}}
    normalized = normalize_options(raw)
    assert normalized['section_order'] == ['rationale', 'data', 'scores', 'grades', 'extras']
    assert raw['options']['section_order'] == ['rationale', 'data']
    assert 'section_order' not in normalize_options({})


@pytest.mark.parametrize('order', [None, {}, 'data', ['data', 'data'], ['unknown'], [1], [{}]])
def test_invalid_section_order_is_rejected(order):
    from xgen_sdk.approval.evaluation import normalize_options
    with pytest.raises(ValueError, match='순서'):
        normalize_options({'options': {'section_order': order}})


def test_typed_values_and_chained_calculation_are_not_mutated():
    before = copy.deepcopy(VALUES)
    assert evaluation_value_errors(FIELDS, VALUES) == []
    values, errors = computed_values(FIELDS[::-1], VALUES)
    assert not errors
    assert values['sum'] == 11 and values['chain'] == 4
    assert VALUES == before


def test_unchecked_checkbox_is_persisted_as_false_and_numeric_overflow_is_rejected():
    values, errors = computed_values(FIELDS, {key: value for key, value in VALUES.items() if key != 'check'})
    assert values['check'] is False and not errors
    definitions = [field('a', 'number'), field('total', 'formula', expression='[a] * [a]')]
    assert evaluation_value_errors(definitions, {'a': 10 ** 300})
    assert evaluation_value_errors(definitions, {'a': 10 ** 400})


def test_empty_typed_values_are_null_not_strings_and_checkbox_stays_boolean():
    definitions = [dict(field('number', 'number'), required=False), dict(field('date', 'date'), required=False),
                   dict(field('select', 'select', choices=[{'code':'a','label':'A'}]), required=False)]
    values, errors = computed_values(definitions, {'number':' ', 'date':'', 'select':''})
    assert not errors and values == {'number':None, 'date':None, 'select':None}
    assert evaluation_value_errors(definitions, values) == []
    assert evaluation_value_errors(FIELDS, dict(VALUES, check=''))


@pytest.mark.parametrize('key,value', [('a', True), ('a', '2'), ('a', math.inf), ('a', math.nan), ('day', '2025-02-29'), ('day', '2024-2-9'), ('choice', 'unknown'), ('check', 'false')])
def test_invalid_typed_values_are_rejected(key, value):
    assert evaluation_value_errors(FIELDS, dict(VALUES, **{key: value}))


@pytest.mark.parametrize('expression', ['[missing]', '[day]+1', '[sum]+1', '1/0', '__import__("os")', '2 ** 4', '2 (3)', '(' * 40 + '1' + ')' * 40])
def test_bad_formula_rejected(expression):
    definitions = copy.deepcopy(FIELDS)
    definitions[5]['config']['expression'] = expression
    try:
        evaluation_fields(definitions)
    except ValueError:
        return
    assert evaluation_value_errors(definitions, VALUES)


def test_cycle_and_removed_reference_are_not_saved():
    definitions = copy.deepcopy(FIELDS)
    definitions[5]['config']['expression'] = '[chain]'
    with pytest.raises(ValueError, match='순환'):
        evaluation_fields(definitions)
    with pytest.raises(ValueError, match='참조'):
        evaluation_fields(FIELDS[1:])


def test_blank_is_not_zero_but_zero_is_a_value():
    defs = [field('a', 'number'), field('out', 'formula', expression='[a] * 2')]
    assert computed_values(defs, {})[0]['out'] is None
    assert computed_values(defs, {'a': 0})[0]['out'] == 0
    assert evaluation_value_errors(defs, {})
    assert not evaluation_value_errors(defs, {}, required=False)


def test_server_uses_persisted_version_and_ignores_forged_result():
    db = EvalDB(); db.add_template('typed', is_default=True)
    options = dict(mitigation=False, impact_scope=False, traits=False, attachments=False, agent_risk_grade=False,
                   custom_fields=FIELDS, sections=dict(scores=False, grades=False, rationale=False, extras=False))
    policy = db.add_policy('typed', 1, options, is_active=False)
    db.add_policy('typed', 2, dict(options, custom_fields=[]))
    data = store._with_server_options(db, {'block_type': 'evaluation'}, {'policy_id': policy, 'options': {}, 'custom_values': VALUES})
    assert data['custom_values']['sum'] == 11
    assert data['custom_values']['chain'] == 4
    assert data['options']['custom_fields'] == FIELDS
