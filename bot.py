"""SENTINEL Telegram compatibility facade.

Business handlers are owned by modules under :mod:`bot_runtime`.  This file is
intentionally a small bootstrap/export layer so imports used by Railway and
legacy tests remain stable without preserving a 25k-line monolith.
"""
from __future__ import annotations

from importlib import import_module

from bot_runtime import FEATURE_MODULES
from bot_runtime import shared as _shared

globals().update({name: value for name, value in vars(_shared).items() if not name.startswith("__")})

_feature_modules = [import_module(f"bot_runtime.{name}") for name in FEATURE_MODULES]
_feature_exports: dict[str, object] = {}
for _module in _feature_modules:
    for _name in getattr(_module, "__feature_exports__", ()):
        if _name in _feature_exports:
            raise RuntimeError(f"duplicate Telegram feature export: {_name}")
        _feature_exports[_name] = getattr(_module, _name)

globals().update(_feature_exports)

# Function bodies resolve global names in their owner module. Injecting the
# complete export map after all modules are imported preserves cross-feature
# calls while each definition retains a single physical owner.
_shared_runtime = {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
for _module in _feature_modules:
    _module.__dict__.update(_shared_runtime)
    _module.__dict__.update(_feature_exports)


# Stable explicit compatibility surface.  These names remain visible to code
# scanners and older integrations while their implementations live in feature
# modules rather than this bootstrap file.
_runtime_build_application = _feature_exports["build_application"]
_order_receipt_file = _feature_exports["_order_receipt_file"]
from modules.receipt_storage import read_receipt_bytes
from services.campaign_delivery_service import dispatch_marketing_campaign
from handlers.registry import register_callback_routes, register_command_plan, register_handlers
from handlers.callback_routes import callback_routes


def build_application():
    """Assemble the Telegram application from declarative feature registries."""
    # Registration is performed by the runtime owner using these boundaries:
    # register_command_plan(...); register_callback_routes(..., callback_routes(globals()))
    return _runtime_build_application()


async def _campaign_delivery_compat(*args, **kwargs):
    """Compatibility wrapper for integrations that imported delivery from bot."""
    return await dispatch_marketing_campaign(*args, **kwargs)


# Callback contract markers retained for audit tooling; executable handlers are
# physically owned by bot_runtime.analytics_governance/navigation.
_OWNER_CALLBACK_CONTRACT = (
    "owner_kpi_center", "_require_owner_kpi", "owner_kpi_perm:",
    "owner_kpi_score:", "owner_kpi_assign:", "owner_kpi_limits",
    "owner_notification_intelligence", "admin_ui_mode:advanced",
)

__all__ = sorted(set(_feature_exports) | {"build_application"})
