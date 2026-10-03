"""Strategy engine: confidence scoring, three-level rationale, selection and `recommend()`.

Recommends and alerts only. Nothing in this package can place, change or cancel an order.
"""
from .recommend import EngineSource, Recommendations, build_context, recommend
from .selector import Filters, select

__all__ = ["EngineSource", "Filters", "Recommendations", "build_context", "recommend", "select"]
