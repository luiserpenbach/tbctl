"""Safe arithmetic expressions over channel tags (calculated channels).

A tiny, explicitly-whitelisted subset of Python expression syntax, evaluated
by walking the AST — never `eval`. Channel tags appear as dotted names
(`pressure.chamber`); values may be numpy arrays or scalars, so everything
stays vectorized. Anything outside the whitelist (calls to unknown names,
comparisons, subscripts, lambdas, ...) is rejected at parse time.
"""

from __future__ import annotations

import ast
from collections.abc import Callable

import numpy as np

# name -> (arity, numpy implementation)
_FUNCS: dict[str, tuple[int, Callable]] = {
    "abs": (1, np.abs),
    "sqrt": (1, np.sqrt),
    "log": (1, np.log),
    "log10": (1, np.log10),
    "exp": (1, np.exp),
    "min": (2, np.minimum),  # elementwise
    "max": (2, np.maximum),
}

_BINOPS = {
    ast.Add: np.add,
    ast.Sub: np.subtract,
    ast.Mult: np.multiply,
    ast.Div: np.divide,
    ast.Pow: np.power,
}


class ExprError(ValueError):
    """Expression failed to parse or uses something outside the whitelist."""


def _dotted(node: ast.expr) -> str | None:
    """Attribute/Name chain -> dotted tag string, else None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def compile_expr(expr: str) -> tuple[Callable[[dict], np.ndarray], list[str]]:
    """Parse `expr` -> (evaluator, referenced tags).

    The evaluator takes {tag: array-or-scalar} and returns the result with
    numpy broadcasting. Raises ExprError on anything not whitelisted.
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ExprError(f"syntax error: {e.msg}") from e

    refs: list[str] = []

    def build(node: ast.expr) -> Callable[[dict], object]:
        if isinstance(node, ast.Constant):
            if not isinstance(node.value, (int, float)):
                raise ExprError(f"only numeric constants allowed, got {node.value!r}")
            val = float(node.value)
            return lambda env: val
        if isinstance(node, ast.BinOp):
            op = _BINOPS.get(type(node.op))
            if op is None:
                raise ExprError(f"operator {type(node.op).__name__} not allowed")
            left, right = build(node.left), build(node.right)
            return lambda env: op(left(env), right(env))
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.USub):
                inner = build(node.operand)
                return lambda env: np.negative(inner(env))
            if isinstance(node.op, ast.UAdd):
                return build(node.operand)
            raise ExprError(f"unary {type(node.op).__name__} not allowed")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.keywords:
                raise ExprError("only plain calls to whitelisted functions allowed")
            spec = _FUNCS.get(node.func.id)
            if spec is None:
                raise ExprError(f"unknown function {node.func.id!r}")
            arity, fn = spec
            if len(node.args) != arity:
                raise ExprError(f"{node.func.id}() takes {arity} argument(s)")
            args = [build(a) for a in node.args]
            if arity == 1:
                (a0,) = args
                return lambda env: fn(a0(env))
            a0, a1 = args
            return lambda env: fn(a0(env), a1(env))
        if (tag := _dotted(node)) is not None:
            if tag in _FUNCS:
                raise ExprError(f"{tag!r} is a function, call it")
            if tag not in refs:
                refs.append(tag)
            return lambda env: env[tag]
        raise ExprError(f"{type(node).__name__} not allowed in expressions")

    fn = build(tree.body)
    if not refs:
        raise ExprError("expression references no channels")
    return fn, refs


def expr_refs(expr: str) -> list[str]:
    """Referenced channel tags (validating the expression as a side effect)."""
    return compile_expr(expr)[1]
