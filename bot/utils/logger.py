"""Structured logging for the bot."""
import logging
import sys
from bot.config import LOG_LEVEL


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
        handler = logging.StreamHandler(sys.stdout)
        fmt = logging.Formatter(
            "[%(asctime)s] %(levelname)-7s %(name)-25s | %(message)s",
            datefmt="%H:%M:%S",
        )
        handler.setFormatter(fmt)
        logger.addHandler(handler)
        logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))
    return logger


def sanitize_reason_for_ui(reason: str) -> str:
    """
    Sanitize action reasoning to hide strategy details from other players.
    Local logs keep full reasoning, but UI gets generic messages.
    """
    # Generic action descriptions that don't reveal strategy
    generic_actions = [
        "Exploring area",
        "Moving position", 
        "Searching for items",
        "Resting",
        "Using item",
        "Equipping gear",
        "Attacking target",
        "Picking up item",
        "Interacting with facility"
    ]
    
    # Map specific reasons to generic ones
    reason_lower = reason.lower()
    
    if any(word in reason_lower for word in ["heal", "hp", "health", "critical"]):
        return "Using item"
    elif any(word in reason_lower for word in ["move", "flee", "escape", "run"]):
        return "Moving position"
    elif any(word in reason_lower for word in ["attack", "fight", "combat", "kill"]):
        return "Attacking target"
    elif any(word in reason_lower for word in ["pickup", "collect", "take"]):
        return "Picking up item"
    elif any(word in reason_lower for word in ["equip", "weapon", "gear"]):
        return "Equipping gear"
    elif any(word in reason_lower for word in ["rest", "ep", "energy"]):
        return "Resting"
    elif any(word in reason_lower for word in ["interact", "facility", "cache"]):
        return "Interacting with facility"
    else:
        return "Exploring area"
