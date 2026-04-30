"""
Unified WebSocket Join for Molty Royale v1.6.0
Handles both free and paid room entry via wss://cdn.moltyroyale.com/ws/join

Per skill.md v1.6.0 Core Rules:
- Single-socket join: hello frame on /ws/join
- Same socket becomes gameplay connection (no re-dial)
- Resume gameplay: direct /ws/agent if IN_GAME
"""
import json
import asyncio
import websockets
from typing import Optional, Tuple
from bot.api_client import MoltyAPI
from bot.credentials import get_api_key, get_agent_private_key
from bot.web3.eip712_signer import sign_join_paid
from bot.config import WS_JOIN_URL, PAID_ENTRY_FEE_SMOLTZ, SKILL_VERSION
from bot.utils.logger import get_logger

log = get_logger(__name__)


class JoinError(Exception):
    """Raised when join fails with specific error code."""
    def __init__(self, message: str, code: str = None):
        super().__init__(message)
        self.code = code


async def join_via_websocket(
    api: MoltyAPI,
    entry_type: str = "free",
    mode: Optional[str] = None
) -> Tuple[str, str, websockets.WebSocketClientProtocol]:
    """
    Unified join via /ws/join WebSocket.
    
    Args:
        api: MoltyAPI instance
        entry_type: "free" or "paid"
        mode: "offchain" or "onchain" (paid only)
    
    Returns:
        (game_id, agent_id, websocket_socket)
        The socket is already in gameplay mode - reuse it!
    
    Raises:
        JoinError: If join fails with specific error
    """
    api_key = get_api_key()
    headers = {
        "Authorization": f"mr-auth {api_key}",
        "X-Version": SKILL_VERSION,
    }
    
    log.info("Opening unified join WebSocket: %s (entry_type=%s)", WS_JOIN_URL, entry_type)
    
    try:
        ws = await websockets.connect(WS_JOIN_URL, additional_headers=headers)
    except Exception as e:
        raise JoinError(f"WebSocket handshake failed: {e}", "HANDSHAKE_FAILED")
    
    try:
        # Step 1: Read welcome frame
        welcome_raw = await ws.recv()
        welcome = json.loads(welcome_raw)
        
        if welcome.get("type") != "welcome":
            raise JoinError(f"Expected welcome, got: {welcome.get('type')}", "UNEXPECTED_FRAME")
        
        decision = welcome.get("decision")
        readiness = welcome.get("readiness", {})
        hello_deadline = welcome.get("helloDeadlineSec", 15)
        
        log.info("Welcome received: decision=%s, deadline=%ss", decision, hello_deadline)
        
        # Check readiness blocks
        if decision == "BLOCKED":
            missing = readiness.get(f"{entry_type}Room", {}).get("missing", [])
            codes = [m.get("code") for m in missing]
            raise JoinError(f"Join blocked: {codes}", codes[0] if codes else "BLOCKED")
        
        # Already in game - socket will proxy to existing game
        if decision == "ALREADY_IN_GAME":
            log.info("Already in game - socket will proxy to existing session")
            # Read the first gameplay frame to get game_id/agent_id
            msg_raw = await ws.recv()
            msg = json.loads(msg_raw)
            
            if msg.get("type") == "agent_view":
                view = msg.get("view", {})
                game_id = view.get("gameId", "")
                agent_id = view.get("self", {}).get("agentId", "")
                return game_id, agent_id, ws
            elif msg.get("type") == "waiting":
                # Need to wait for agent_view
                return "", "", ws  # Will be resolved in WebSocketEngine
        
        # Check if entry type is allowed
        instruction = welcome.get("instruction", {})
        entry_instruction = instruction.get(entry_type, {})
        
        if not entry_instruction.get("enabled", False):
            blocked_reason = entry_instruction.get("blockedReason", "not enabled")
            raise JoinError(f"{entry_type} not allowed: {blocked_reason}", "ENTRYTYPE_NOT_PERMITTED")
        
        # Step 2: Send hello frame
        hello_msg = {"type": "hello", "entryType": entry_type}
        if entry_type == "paid" and mode:
            hello_msg["mode"] = mode
        
        log.info("Sending hello: %s", hello_msg)
        await ws.send(json.dumps(hello_msg))
        
        # Step 3: Handle entry-type specific flows
        if entry_type == "free":
            return await _handle_free_flow(ws)
        else:
            return await _handle_paid_flow(ws, api)
            
    except Exception:
        # Clean up socket on error
        await ws.close()
        raise


async def _handle_free_flow(ws: websockets.WebSocketClientProtocol) -> Tuple[str, str, websockets.WebSocketClientProtocol]:
    """Handle free room join flow: queued -> assigned"""
    while True:
        msg_raw = await ws.recv()
        msg = json.loads(msg_raw)
        msg_type = msg.get("type")
        
        log.debug("Free join frame: %s", msg_type)
        
        if msg_type == "queued":
            log.info("In matchmaking queue...")
            continue
        
        elif msg_type == "assigned":
            game_id = msg.get("gameId", "")
            agent_id = msg.get("agentId", "")
            log.info("✅ Assigned to free game: %s (agent=%s)", game_id, agent_id)
            return game_id, agent_id, ws
        
        elif msg_type == "not_selected":
            raise JoinError("Not selected in this matchmaking cycle", "NOT_SELECTED")
        
        elif msg_type == "error":
            code = msg.get("code", "UNKNOWN")
            raise JoinError(f"Join error: {code}", code)
        
        elif msg_type in ("agent_view", "waiting"):
            # Sometimes assigned flows directly into gameplay
            if msg_type == "agent_view":
                view = msg.get("view", {})
                game_id = view.get("gameId", "")
                agent_id = view.get("self", {}).get("agentId", "")
                return game_id, agent_id, ws
            return "", "", ws


async def _handle_paid_flow(ws: websockets.WebSocketClientProtocol, api: MoltyAPI) -> Tuple[str, str, websockets.WebSocketClientProtocol]:
    """Handle paid room join flow: sign_required -> sign_submit -> queued -> tx_submitted -> joined"""
    while True:
        msg_raw = await ws.recv()
        msg = json.loads(msg_raw)
        msg_type = msg.get("type")
        
        log.debug("Paid join frame: %s", msg_type)
        
        if msg_type == "sign_required":
            join_intent_id = msg.get("joinIntentId", "")
            deadline = msg.get("deadline", 0)
            eip712_data = msg.get("message", {})
            
            log.info("Sign required for joinIntentId=%s", join_intent_id)
            
            # Sign with agent EOA
            agent_pk = get_agent_private_key()
            if not agent_pk:
                raise JoinError("Agent private key not found", "NO_PRIVATE_KEY")
            
            try:
                signature = sign_join_paid(agent_pk, eip712_data)
            except Exception as e:
                raise JoinError(f"Signing failed: {e}", "SIGN_FAILED")
            
            # Submit signature
            submit_msg = {
                "type": "sign_submit",
                "joinIntentId": join_intent_id,
                "signature": signature
            }
            log.info("Submitting signature...")
            await ws.send(json.dumps(submit_msg))
            continue
        
        elif msg_type == "queued":
            log.info("Paid join queued, waiting for tx...")
            continue
        
        elif msg_type == "tx_submitted":
            tx_hash = msg.get("txHash", "")
            log.info("Paid join tx submitted: %s", tx_hash)
            continue
        
        elif msg_type == "joined":
            game_id = msg.get("gameId", "")
            agent_id = msg.get("agentId", "")
            log.info("✅ Joined paid game: %s (agent=%s)", game_id, agent_id)
            return game_id, agent_id, ws
        
        elif msg_type == "error":
            code = msg.get("code", "UNKNOWN")
            raise JoinError(f"Paid join error: {code}", code)
        
        elif msg_type in ("agent_view", "waiting"):
            # Direct to gameplay
            if msg_type == "agent_view":
                view = msg.get("view", {})
                game_id = view.get("gameId", "")
                agent_id = view.get("self", {}).get("agentId", "")
                return game_id, agent_id, ws
            return "", "", ws


async def check_readiness_from_welcome(api: MoltyAPI) -> dict:
    """
    Quick check via /ws/join welcome frame to get readiness status.
    Returns welcome frame without proceeding to join.
    """
    api_key = get_api_key()
    headers = {
        "Authorization": f"mr-auth {api_key}",
        "X-Version": SKILL_VERSION,
    }
    
    try:
        ws = await websockets.connect(WS_JOIN_URL, additional_headers=headers)
        welcome_raw = await ws.recv()
        welcome = json.loads(welcome_raw)
        await ws.close()
        return welcome
    except Exception as e:
        log.warning("Failed to check readiness via /ws/join: %s", e)
        return {}
