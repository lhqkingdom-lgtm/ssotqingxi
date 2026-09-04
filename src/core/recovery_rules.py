from __future__ import annotations

from typing import Any, Iterable, Mapping


def recover_configured_value(
    value: str,
    context: Mapping[str, Any],
    rules: Iterable[Mapping[str, Any]],
) -> str:
    """Apply declarative recovery rules to a damaged extracted value.

    A rule may constrain the current value and require one or more context
    tokens.  The executor is document-family agnostic; semantic mappings live
    in the selected profile.
    """
    normalized_value = value.upper()
    context_tokens = {
        str(token).upper()
        for tokens in context.values()
        for token in (tokens if isinstance(tokens, (list, tuple, set)) else [tokens])
        if token is not None
    }
    for rule in rules:
        current_values = {str(item).upper() for item in rule.get("current_value_in", [])}
        required_any = {str(item).upper() for item in rule.get("context_any", [])}
        required_all = {str(item).upper() for item in rule.get("context_all", [])}
        if current_values and normalized_value not in current_values:
            continue
        if required_any and not required_any.intersection(context_tokens):
            continue
        if required_all and not required_all.issubset(context_tokens):
            continue
        replacement = rule.get("replacement")
        if replacement:
            return str(replacement).upper()
    return normalized_value
