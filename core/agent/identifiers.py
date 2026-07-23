"""QGIS-free shared identity/limit constants for the agent proposal boundary.

These are the fixed proposal-kind / target-identity strings and the style
signing-state limit that the read-only inspection tools, the runtime validator,
and the runtime apply coordinator must all agree on. Keeping them here (instead
of in the QGIS-importing ``runtime_tools``) lets the pure and model-apply paths
import them without pulling in ``qgis.core``.
"""
from __future__ import annotations

from .context import MAX_LIST_ITEMS

# Identity of the single current-model target and the fixed proposal kinds each
# inspection tool's freshness receipt is bound to.
MODEL_TARGET_ID = "current_model"
MODEL_PROPOSAL_KIND = "model_patch"
STYLE_PROPOSAL_KIND = "layer_style"
# A ``processing_run`` receipt is issued by ``processing.describe`` and bound to
# the algorithm id plus its live signature, so a provider/registry update (or a
# different algorithm) invalidates it. A ``model_run`` reuses the model receipt
# above: it names no algorithm and runs only the current graph.
PROCESSING_PROPOSAL_KIND = "processing_run"

# Display-limit-independent bound used only for the style *freshness* signing
# state, so the receipt depends on the layer's meaningful style, not on the
# caller's chosen ``limit``.
STYLE_STATE_LIMIT = MAX_LIST_ITEMS
