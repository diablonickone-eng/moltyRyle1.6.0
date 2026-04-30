"""
State router — determines agent state from GET /accounts/me response.
Routes per skill.md State Router logic.

v1.6.0 updates:
- SC Wallet policy checks (NOT_PRIMARY_AGENT, ACTIVE_*_GAME_EXISTS)
- Unified readiness checking via /ws/join welcome frame
"""
from bot.utils.logger import get_logger

log = get_logger(__name__)

# States
NO_ACCOUNT = "NO_ACCOUNT"
NO_IDENTITY = "NO_IDENTITY"
IN_GAME = "IN_GAME"
READY_PAID = "READY_PAID"
READY_FREE = "READY_FREE"
ERROR = "ERROR"

# v1.6.0 SC Wallet Policy States
SC_WALLET_BLOCKED = "SC_WALLET_BLOCKED"  # NOT_PRIMARY_AGENT or other policy violation


def determine_state(me_response: dict) -> tuple[str, dict]:
    """
    Analyze /accounts/me response → return (state, context).
    Context contains relevant data for the next step.
    
    v1.6.0: Also checks SC wallet policy violations in readiness.
    """
    readiness = me_response.get("readiness", {})
    current_games = me_response.get("currentGames", [])
    
    # v1.6.0: Check SC Wallet Policy violations first
    sc_wallet_issues = _check_sc_wallet_policy(readiness)
    if sc_wallet_issues:
        log.warning("SC Wallet policy violation: %s", sc_wallet_issues)
        return SC_WALLET_BLOCKED, {
            "issues": sc_wallet_issues,
            "balance": me_response.get("balance", 0),
        }

    # Check for active game
    for game in current_games:
        if game.get("gameStatus") in ("waiting", "running"):
            log.info("Active game found: %s (status=%s)",
                     game["gameId"], game["gameStatus"])
            return IN_GAME, {
                "game_id": game["gameId"],
                "agent_id": game["agentId"],
                "game_status": game["gameStatus"],
                "entry_type": game.get("entryType", "free"),
                "is_alive": game.get("isAlive", True),
            }

    # Check ERC-8004 identity
    erc8004_id = readiness.get("erc8004Id")
    if erc8004_id is None:
        log.info("No ERC-8004 identity registered")
        return NO_IDENTITY, {}

    # v1.6.0: Check detailed readiness from /ws/join perspective
    free_room_ready = _check_free_room_readiness(readiness)
    paid_room_ready = _check_paid_room_readiness(readiness, me_response.get("balance", 0))

    # Check paid readiness (v1.6.0: uses paidRoom.readiness structure)
    if paid_room_ready:
        balance = me_response.get("balance", 0)
        log.info("Paid ready: balance=%d sMoltz", balance)
        return READY_PAID, {
            "balance": balance,
            "mode": "offchain",  # Default; could check onchain availability
        }

    # Default to free
    log.info("Ready for free play (freeRoom.ready=%s)", free_room_ready)
    return READY_FREE, {
        "balance": me_response.get("balance", 0),
        "wallet_address": readiness.get("walletAddress"),
        "whitelist_approved": readiness.get("whitelistApproved", False),
        "free_room_ready": free_room_ready,
        "paid_room_ready": paid_room_ready,
    }


def _check_sc_wallet_policy(readiness: dict) -> list:
    """
    v1.6.0: Check SC Wallet Policy violations.
    Returns list of issue codes if any violations found.
    
    Per sc-wallet-policy.md:
    - NOT_PRIMARY_AGENT: agent not primary for SC wallet
    - ACTIVE_FREE_GAME_EXISTS: another agent from same SC wallet in free game
    - ACTIVE_PAID_GAME_EXISTS: another agent from same SC wallet in paid game
    """
    issues = []
    
    # Check free room readiness
    free_room = readiness.get("freeRoom", {})
    if not free_room.get("ok", True):
        for missing in free_room.get("missing", []):
            code = missing.get("code", "")
            if code in ("NOT_PRIMARY_AGENT", "ACTIVE_FREE_GAME_EXISTS", "ACTIVE_PAID_GAME_EXISTS"):
                issues.append({
                    "code": code,
                    "context": "freeRoom",
                    "guide": missing.get("guide", ""),
                })
    
    # Check paid room readiness
    paid_room = readiness.get("paidRoom", {})
    if not paid_room.get("ok", True):
        for missing in paid_room.get("missing", []):
            code = missing.get("code", "")
            if code in ("NOT_PRIMARY_AGENT", "ACTIVE_PAID_GAME_EXISTS", "ACTIVE_FREE_GAME_EXISTS"):
                issues.append({
                    "code": code,
                    "context": "paidRoom",
                    "guide": missing.get("guide", ""),
                })
    
    return issues


def _check_free_room_readiness(readiness: dict) -> bool:
    """
    v1.6.0: Check if free room is ready based on readiness structure.
    """
    free_room = readiness.get("freeRoom", {})
    if isinstance(free_room, dict):
        return free_room.get("ok", True) and not free_room.get("missing", [])
    # Legacy fallback
    return True


def _check_paid_room_readiness(readiness: dict, balance: int) -> bool:
    """
    v1.6.0: Check if paid room is ready.
    Requires: balance >= 500, whitelist approved, no active paid game
    """
    paid_room = readiness.get("paidRoom", {})
    if isinstance(paid_room, dict):
        # v1.6.0 structure: { ok: bool, mode: {...}, missing: [...] }
        if not paid_room.get("ok", False):
            return False
        # Also check legacy paidReady flag
        return readiness.get("paidReady", False) and balance >= 500
    
    # Legacy fallback
    return readiness.get("paidReady", False) and balance >= 500


def format_sc_wallet_error(issues: list) -> str:
    """
    Format SC wallet policy violations for user notification.
    """
    if not issues:
        return ""
    
    error_parts = []
    for issue in issues:
        code = issue.get("code", "")
        guide = issue.get("guide", "")
        
        if code == "NOT_PRIMARY_AGENT":
            error_parts.append(
                "This agent is not the primary agent for its SC wallet. "
                "Only the primary agent (smallest accounts.id) can play. "
                "Check My Agent page to verify primary status."
            )
        elif code == "ACTIVE_FREE_GAME_EXISTS":
            error_parts.append(
                "Another agent from this SC wallet is already in an active free game. "
                "Wait for that game to finish before joining."
            )
        elif code == "ACTIVE_PAID_GAME_EXISTS":
            error_parts.append(
                "Another agent from this SC wallet is already in an active paid game. "
                "Only 1 free + 1 paid game per SC wallet allowed."
            )
        else:
            error_parts.append(f"{code}: {guide}")
    
    return " | ".join(error_parts)
