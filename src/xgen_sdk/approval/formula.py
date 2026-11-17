"""Bounded arithmetic for evaluation fields; never executes user code."""
import math
import re


def finite_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def parse_formula(expression):
    if not isinstance(expression, str) or not expression.strip() or len(expression) > 1000:
        raise ValueError("계산식은 1~1000자로 입력하세요.")
    tokens = []
    pos = 0
    pattern = re.compile(r"\s*(\[[A-Za-z0-9_-]{1,100}\]|(?:\d+(?:\.\d*)?|\.\d+)|[()+*/-])")
    while pos < len(expression.rstrip()):
        match = pattern.match(expression, pos)
        if not match or len(tokens) >= 256:
            raise ValueError("계산식에는 숫자, 항목 참조, 사칙연산과 괄호만 사용할 수 있습니다.")
        tokens.append(match[1])
        pos = match.end()
    index = 0

    def expr(depth=0, minimum=0):
        nonlocal index
        if depth > 32 or index >= len(tokens):
            raise ValueError("계산식의 괄호와 연산자를 확인하세요.")
        token = tokens[index]
        index += 1
        if token in ("+", "-"):
            node = ("unary", token, expr(depth + 1, 3))
        elif token == "(":
            node = expr(depth + 1)
            if index >= len(tokens) or tokens[index] != ")":
                raise ValueError("계산식의 괄호를 확인하세요.")
            index += 1
        elif token.startswith("["):
            node = ("ref", token[1:-1])
        elif token[0].isdigit() or token[0] == ".":
            number = float(token)
            if not math.isfinite(number):
                raise ValueError("계산식 숫자가 너무 큽니다.")
            node = ("number", number)
        else:
            raise ValueError("계산식의 연산자를 확인하세요.")
        while index < len(tokens):
            op = tokens[index]
            precedence = {"+": 1, "-": 1, "*": 2, "/": 2}.get(op, 0)
            if not precedence or precedence < minimum:
                break
            index += 1
            node = ("binary", op, node, expr(depth + 1, precedence + 1))
        return node

    tree = expr()
    if index != len(tokens):
        raise ValueError("계산식의 연산자를 확인하세요.")
    return tree


def references(tree):
    if tree[0] == "ref":
        return {tree[1]}
    if tree[0] == "unary":
        return references(tree[2])
    if tree[0] == "binary":
        return references(tree[2]) | references(tree[3])
    return set()


def calculate(tree, resolve):
    kind = tree[0]
    if kind == "number":
        return tree[1]
    if kind == "ref":
        value = resolve(tree[1])
        if value is None or value == "":
            return None
        if not finite_number(value):
            raise ValueError("참조 항목에 올바른 숫자를 입력하세요.")
        return value
    left = calculate(tree[2], resolve)
    if kind == "unary":
        return None if left is None else (-left if tree[1] == "-" else left)
    right = calculate(tree[3], resolve)
    if left is None or right is None:
        return None
    op = tree[1]
    if op == "/" and right == 0:
        raise ValueError("0으로 나눌 수 없습니다.")
    result = left + right if op == "+" else left - right if op == "-" else left * right if op == "*" else left / right
    if not finite_number(result):
        raise ValueError("계산 결과가 숫자 범위를 초과했습니다.")
    return result


def formula_trees(fields):
    by_id = {field["id"]: field for field in fields}
    trees = {field["id"]: parse_formula(field["config"].get("expression")) for field in fields if field["type"] == "formula"}
    done, active = set(), set()

    def visit(key):
        if key in active:
            raise ValueError("계산식에 순환 참조가 있습니다.")
        if key in done or key not in trees:
            return
        active.add(key)
        for ref in references(trees[key]):
            if ref not in by_id or by_id[ref]["type"] not in ("number", "formula"):
                raise ValueError("계산식은 존재하는 숫자 또는 계산 항목만 참조할 수 있습니다.")
            visit(ref)
        active.remove(key)
        done.add(key)

    for key in trees:
        visit(key)
    return trees


def computed_values(fields, values):
    trees = formula_trees(fields)
    result = {}
    for field in fields:
        value = values.get(field["id"])
        if field["type"] == "checkbox" and value is None:
            value = False
        elif field["type"] in ("number", "date", "select") and isinstance(value, str) and not value.strip():
            value = None
        result[field["id"]] = value
    errors, done = {}, set()

    def resolve(key):
        if key in trees and key not in done:
            done.add(key)
            try:
                result[key] = calculate(trees[key], resolve)
            except ValueError as exc:
                result[key] = None
                errors[key] = str(exc)
        return result.get(key)

    for key in trees:
        resolve(key)
    return result, errors
