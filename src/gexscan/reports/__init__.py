"""Engine outputs: the recommendation brief (console / markdown / JSON). Recommends only; no execution step."""
from .brief import markdown, print_brief, print_explain, print_idea, to_json

__all__ = ["markdown", "print_brief", "print_explain", "print_idea", "to_json"]
