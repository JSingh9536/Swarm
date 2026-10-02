from __future__ import annotations

from pathlib import Path

from swarm.models import WorkItem


def classify_tier(item: WorkItem, project_dir: Path) -> int:
    """
    Mechanically classify a work item into a tier:
    Tier 1: Local model (Ollama) - Simple, low-risk, easily verified.
    Tier 2: Claude model - Complex, multi-file, or high-risk.
    
    Heuristics for Tier 1:
    1. Change <= 1 file (checked via content or title if available, otherwise defaults to Tier 2).
    2. Has a simple, single verification command.
    3. Is purely documentation.
    """
    # Documentation is always Tier 1
    if "docs" in item.title.lower() or "readme" in item.title.lower():
        return 1
        
    # In a real scenario, we would analyze the WorkItem's description or the 
    # architectural plan to see how many files are targeted.
    # For now, if the title suggests a single fix or a tiny change, we'll lean Tier 1.
    # If it's a 'build' or 'refactor' of a whole module, it's Tier 2.
    
    lower_title = item.title.lower()
    if any(word in lower_title for word in ("build", "refactor", "implement", "architect")):
        return 2
        
    if "fix" in lower_title or "update" in lower_title:
        return 1
        
    return 2 # Default to safe (Claude)
