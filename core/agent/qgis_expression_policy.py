"""Live QGIS-expression validation for reviewed Agent Processing runs.

Expressions are parsed by QGIS itself and are never evaluated during proposal
validation. The later approved Processing run may evaluate only expressions
which passed this gate. This keeps QGIS's ordinary field, geometry, math,
string, date/time, array, map, aggregate and overlay language available while
blocking custom Python functions and local environment/filesystem discovery.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any, FrozenSet

from qgis.core import QgsExpression, QgsVectorLayer


_BLOCKED_FUNCTIONS: FrozenSet[str] = frozenset(
    {
        "decode_uri",
        "env",
        "eval",
        "file_exists",
        "file_name",
        "file_path",
        "file_size",
        "file_suffix",
        "layer_property",
    }
)
_BLOCKED_FUNCTION_GROUPS: FrozenSet[str] = frozenset(
    {"custom", "files and paths"}
)
_SENSITIVE_VARIABLE_TERMS = (
    "credential",
    "folder",
    "home",
    "password",
    "path",
    "secret",
    "token",
)


@dataclass(frozen=True)
class ExpressionValidation:
    ok: bool
    message: str = ""


def _registered_functions() -> dict:
    functions = {}
    for function in QgsExpression.Functions():
        with contextlib.suppress(AttributeError, TypeError, ValueError):
            functions[str(function.name()).casefold()] = function
    return functions


def is_agent_expression_function(function: Any) -> bool:
    """Whether one live registered function is safe for Agent formulas."""

    try:
        name = str(function.name()).casefold()
        group = str(function.group()).casefold()
    except (AttributeError, TypeError, ValueError):
        return False
    return (
        name not in _BLOCKED_FUNCTIONS
        and group not in _BLOCKED_FUNCTION_GROUPS
    )


def validate_qgis_expression(expression_text: str, layer: Any) -> ExpressionValidation:
    """Parse and scope-check one expression without evaluating a feature."""

    if not isinstance(expression_text, str) or not expression_text.strip():
        return ExpressionValidation(False, "A non-empty QGIS expression is required.")

    expression = QgsExpression(expression_text)
    if expression.hasParserError():
        return ExpressionValidation(
            False, "The formula is not valid QGIS expression syntax."
        )

    functions = _registered_functions()
    for name in expression.referencedFunctions():
        normalized = str(name).casefold()
        function = functions.get(normalized)
        if function is None:
            return ExpressionValidation(
                False, "The formula refers to an unavailable QGIS function."
            )
        if not is_agent_expression_function(function):
            return ExpressionValidation(
                False,
                "Custom, dynamic, environment and filesystem expression "
                "functions are not available to Agent runs.",
            )

    try:
        variables = expression.referencedVariables()
    except AttributeError:
        variables = ()
    for variable in variables:
        normalized = str(variable).casefold()
        if any(term in normalized for term in _SENSITIVE_VARIABLE_TERMS):
            return ExpressionValidation(
                False,
                "The formula refers to a path, credential, or secret-like variable.",
            )

    if not isinstance(layer, QgsVectorLayer):
        return ExpressionValidation(
            False, "A QGIS expression run requires one live vector input layer."
        )
    fields = {field.name() for field in layer.fields()}
    referenced = {str(name) for name in expression.referencedColumns()}
    unknown = sorted(name for name in referenced if name != "*" and name not in fields)
    if unknown:
        return ExpressionValidation(
            False, "The formula refers to a field that is not present in the input layer."
        )
    return ExpressionValidation(True)
