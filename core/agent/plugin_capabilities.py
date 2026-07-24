"""Pure, QGIS-free logic for mapping an installed plugin to what it can do.

The hard problem this module solves is answering "does this installed plugin
register Processing algorithms, and which ones?" **without ever touching the
plugin**. Importing it, constructing it, or even reading an attribute off a
loaded plugin instance can execute third-party code (an attribute may be a
property), so none of that is allowed.

The trick is to derive the mapping entirely from the **provider side**. QGIS has
already loaded and registered every live Processing provider; the class of each
provider knows which Python package defined it:

    type(provider).__module__.split(".")[0]

Reading a class's ``__module__`` runs no plugin code. If that package equals the
plugin's package name, the mapping is **proved**. If it does not, we say so --
we never guess a mapping and present it as confirmed.

Everything here operates on small immutable views built by the QGIS adapter in
``runtime_tools.py``, so the honesty rules are unit-testable without QGIS.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import context as agent_context

# Status values. Exactly these five; the UI and the tests depend on them.
NOT_INSTALLED = "not_installed"
CONFIRMED_PROVIDER = "confirmed_provider"
DECLARED_UNCONFIRMED = "declared_unconfirmed"
CANDIDATE_ONLY = "candidate_only"
UI_ONLY_OR_UNMAPPED = "ui_only_or_unmapped"

CONFIDENCE_CONFIRMED = "confirmed"
CONFIDENCE_CANDIDATE = "candidate"
CONFIDENCE_NONE = "none"

MAX_PROVIDERS = 10
MAX_ALGORITHMS = 60
MAX_PROVIDER_TEXT = 128

# Guidance is chosen from this fixed, application-owned table and is never
# assembled from plugin metadata, so untrusted text cannot become instructions.
_GUIDANCE = {
    NOT_INSTALLED: (
        "This package is not installed in QGIS, so nothing can be said about it."
    ),
    CONFIRMED_PROVIDER: (
        "This plugin registers Processing algorithms, listed below. They can be "
        "searched and explained here, but running one is not available in this "
        "version: the agent may only run its own reviewed list of core QGIS "
        "algorithms. Run them yourself from the Processing Toolbox."
    ),
    DECLARED_UNCONFIRMED: (
        "This plugin's metadata says it provides Processing algorithms, but no "
        "live provider could be traced back to it -- it may be disabled, may "
        "have failed to load, or may register under another package. No "
        "algorithm list can be given honestly."
    ),
    CANDIDATE_ONLY: (
        "A Processing provider resembles this plugin's name, but it could not be "
        "proved to come from it, so it is reported as a possibility only and no "
        "algorithm list is given."
    ),
    UI_ONLY_OR_UNMAPPED: (
        "This plugin does not register Processing algorithms, so it can only be "
        "described from its QGIS metadata. Driving its buttons, menus, or dialogs "
        "is not available -- use the plugin's own interface."
    ),
}

_NON_ALPHANUM = re.compile(r"[^a-z0-9]+")

# A resemblance is only worth reporting if it is *specific*. Two guards keep the
# candidate signal honest rather than noisy:
#   - a substring match must involve a reasonably long name, because a short one
#     ("qgis") is a substring of a large fraction of plugin package names;
#   - framework-wide provider names never resemble a particular plugin.
MIN_RESEMBLANCE_CHARS = 6
_GENERIC_PROVIDER_TERMS = frozenset(
    {
        "qgis", "gdal", "grass", "saga", "otb", "native", "processing",
        "script", "scripts", "model", "models", "plugin", "plugins",
        "tools", "toolbox", "lab", "core", "utils",
    }
)


@dataclass(frozen=True)
class ProviderView:
    """A QGIS-free view of one live Processing provider.

    ``owning_package`` is ``type(provider).__module__.split(".")[0]`` -- the
    Python package that *defined* the provider class. It is the only field that
    can prove a mapping.
    """

    provider_id: str
    name: str
    owning_package: str
    algorithms: Tuple[Tuple[str, str, str], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PluginView:
    """A QGIS-free view of one installed plugin's bounded metadata."""

    package_name: str
    display_name: str = ""
    version: str = ""
    enabled: bool = False
    declares_processing_provider: bool = False
    installed: bool = True


def _normalize(text: str) -> str:
    return _NON_ALPHANUM.sub("", str(text).lower())


def _resembles(package_name: str, provider: ProviderView) -> bool:
    """Whether a provider's id/name resembles the package, well enough to be
    worth reporting as a *candidate* -- never as a confirmation."""
    package = _normalize(package_name)
    if len(package) < MIN_RESEMBLANCE_CHARS:
        # Too short to resemble anything without absurd false positives.
        return False
    for text in (provider.provider_id, provider.name):
        normalized = _normalize(text)
        if not normalized or normalized in _GENERIC_PROVIDER_TERMS:
            continue
        if normalized == package:
            return True
        if len(normalized) >= MIN_RESEMBLANCE_CHARS and (
            normalized in package or package in normalized
        ):
            return True
    return False


def _bounded_provider(provider: ProviderView, confirmed: bool) -> Dict[str, Any]:
    return {
        "provider_id": agent_context.bound_text(provider.provider_id, MAX_PROVIDER_TEXT),
        "name": agent_context.bound_text(provider.name, agent_context.MAX_DISPLAY_NAME),
        "confirmed": bool(confirmed),
    }


def build_capabilities(
    plugin: Optional[PluginView],
    providers: Sequence[ProviderView],
    *,
    limit: int = MAX_ALGORITHMS,
    algorithm_allowed: Optional[Any] = None,
) -> Dict[str, Any]:
    """Return the bounded, honest capability report for one installed plugin.

    ``plugin`` is ``None`` (or not installed) when the package is unknown to
    QGIS. ``algorithm_allowed`` is the blocked-id check; algorithms failing it
    are omitted from the listing entirely.
    """
    if plugin is None or not plugin.installed:
        package = plugin.package_name if plugin is not None else ""
        return _report(
            package_name=package,
            plugin=None,
            status=NOT_INSTALLED,
            confidence=CONFIDENCE_NONE,
            providers=[],
            algorithms=[],
            truncated=False,
        )

    confirmed = [p for p in providers if p.owning_package and p.owning_package == plugin.package_name]
    if confirmed:
        rows, truncated = _collect_algorithms(confirmed, limit, algorithm_allowed)
        return _report(
            package_name=plugin.package_name,
            plugin=plugin,
            status=CONFIRMED_PROVIDER,
            confidence=CONFIDENCE_CONFIRMED,
            providers=[_bounded_provider(p, True) for p in confirmed[:MAX_PROVIDERS]],
            algorithms=rows,
            truncated=truncated,
        )

    # Nothing proved. A resemblance may be reported, but only as a candidate and
    # without any algorithm listing -- an unproved provider's algorithms are not
    # this plugin's algorithms as far as we can honestly say.
    candidates = [p for p in providers if _resembles(plugin.package_name, p)]
    if candidates:
        status = CANDIDATE_ONLY
        if plugin.declares_processing_provider:
            # The metadata claim plus an unproved resemblance is still unproved;
            # report the stronger, more honest "declared but unconfirmed".
            status = DECLARED_UNCONFIRMED
        return _report(
            package_name=plugin.package_name,
            plugin=plugin,
            status=status,
            confidence=CONFIDENCE_CANDIDATE,
            providers=[_bounded_provider(p, False) for p in candidates[:MAX_PROVIDERS]],
            algorithms=[],
            truncated=False,
        )

    status = DECLARED_UNCONFIRMED if plugin.declares_processing_provider else UI_ONLY_OR_UNMAPPED
    return _report(
        package_name=plugin.package_name,
        plugin=plugin,
        status=status,
        confidence=CONFIDENCE_NONE,
        providers=[],
        algorithms=[],
        truncated=False,
    )


def _collect_algorithms(
    providers: Sequence[ProviderView], limit: int, algorithm_allowed: Optional[Any]
) -> Tuple[List[Dict[str, str]], bool]:
    rows: List[Dict[str, str]] = []
    for provider in providers:
        for algorithm_id, title, group in provider.algorithms:
            if algorithm_allowed is not None and not algorithm_allowed(algorithm_id):
                continue
            rows.append(
                {
                    "algorithm_id": agent_context.bound_text(algorithm_id, 200),
                    "title": agent_context.bound_text(title, agent_context.MAX_DISPLAY_NAME),
                    "group": agent_context.bound_text(group, agent_context.MAX_DISPLAY_NAME),
                }
            )
    rows.sort(key=lambda row: row["algorithm_id"])
    bounded = max(1, min(int(limit), MAX_ALGORITHMS))
    truncated = len(rows) > bounded
    return rows[:bounded], truncated


def _report(
    *,
    package_name: str,
    plugin: Optional[PluginView],
    status: str,
    confidence: str,
    providers: List[Dict[str, Any]],
    algorithms: List[Dict[str, str]],
    truncated: bool,
) -> Dict[str, Any]:
    return {
        "available": plugin is not None,
        "package_name": agent_context.bound_text(package_name, 128),
        "display_name": agent_context.bound_text(
            (plugin.display_name if plugin else "") or package_name,
            agent_context.MAX_DISPLAY_NAME,
        ),
        "version": agent_context.bound_text(
            plugin.version if plugin else "", agent_context.MAX_SHORT_TEXT
        ),
        "enabled": bool(plugin.enabled) if plugin else False,
        "declares_processing_provider": (
            bool(plugin.declares_processing_provider) if plugin else False
        ),
        "status": status,
        "confidence": confidence,
        "providers": providers,
        "algorithms": algorithms,
        "algorithms_truncated": bool(truncated),
        # Owner decision 2026-07-24: the reviewed run allowlist stays at the
        # twelve core QGIS algorithms, so no plugin algorithm is ever runnable
        # by the agent in V1. Reported here rather than discovered by failure.
        "agent_executable": False,
        "guidance": _GUIDANCE[status],
    }
