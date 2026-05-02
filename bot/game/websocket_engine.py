"""
WebSocket gameplay engine — wss://cdn.moltyroyale.com/ws/agent.
Core loop: connect → process messages → decide → act → repeat.

Per game-loop.md:
- agent_view uses 'view' key (NOT 'data')
- turn_advanced includes full 'view' snapshot — MUST be processed
- action envelope: { type: "action", data: { type: "ACTION_TYPE", ... }, thought: {...} }
- action_result: includes canAct + cooldownRemainingMs at TOP LEVEL
- can_act_changed: canAct at TOP LEVEL (not nested in data)
- Only one WS session per API key
"""
import json
import time
import asyncio
import websockets
from bot.config import WS_URL, SKILL_VERSION
from bot.credentials import get_api_key
from bot.game.action_sender import ActionSender, COOLDOWN_ACTIONS, FREE_ACTIONS
from bot.strategy.brain import decide_action, reset_game_state, learn_from_map
from bot.dashboard.state import dashboard_state
from bot.learning import record_match
from bot.utils.rate_limiter import ws_limiter
from bot.utils.logger import get_logger

log = get_logger(__name__)


def _update_dz_knowledge(view: dict):
    """Continuously track death zones from every agent_view.
    Updates brain._map_knowledge with any new DZ regions observed.
    v1.5.2: pendingDeathzones entries are {id, name} objects.
    """
    from bot.strategy.brain import _map_knowledge
    # Track DZ from visible regions
    for region in view.get("visibleRegions", []):
        if isinstance(region, dict) and region.get("isDeathZone"):
            rid = region.get("id", "")
            if rid:
                _map_knowledge["death_zones"].add(rid)
    # Track from connected regions (type-safe: may be string IDs or objects)
    for conn in view.get("connectedRegions", []):
        if isinstance(conn, dict) and conn.get("isDeathZone"):
            rid = conn.get("id", "")
            if rid:
                _map_knowledge["death_zones"].add(rid)
        # Bare string IDs — we don't know if it's DZ, skip
    # Track current region
    cur = view.get("currentRegion", {})
    if isinstance(cur, dict) and cur.get("isDeathZone"):
        rid = cur.get("id", "")
        if rid:
            _map_knowledge["death_zones"].add(rid)
    # Track pending DZ — v1.5.2: entries are {id, name} objects
    for dz in view.get("pendingDeathzones", []):
        if isinstance(dz, dict):
            rid = dz.get("id", "")
            if rid:
                _map_knowledge["death_zones"].add(rid)
        elif isinstance(dz, str):
            _map_knowledge["death_zones"].add(dz)  # Legacy fallback


class WebSocketEngine:
    """Manages the gameplay WebSocket session."""

    def __init__(self, game_id: str, agent_id: str):
        self.game_id = game_id
        self.agent_id = agent_id
        self.action_sender = ActionSender()
        self.ws = None
        self.game_result = None
        self.last_view = None
        self._ping_task = None
        self._running = False
        self._map_just_used = False  # Track if Map was used for learning
        # Dashboard key/name — set by heartbeat before .run()
        self.dashboard_key = agent_id  # fallback to agent_id
        self.dashboard_name = "Agent"
        self._existing_ws = None  # Socket from unified join (v1.6.0)
        self.last_sent_action = None  # {type, itemId, targetId}
        # Game stats tracking for self-learning
        self._game_stats = {
            "start_time": None,
            "kills": 0,
            "damage_dealt": 0,
            "damage_taken": 0,
            "moltz": 0,
            # NEW: Detailed analytics tracking
            "cause_of_death": None,  # e.g., "combat", "death_zone", "starvation"
            "time_of_death": None,   # Turn number when died
            "last_region_id": None,  # Region where bot died
            "items_used": [],        # List of {typeId, turn, reason}
            "heal_items_used": 0,    # Count of healing items consumed
            "weapon_switches": 0,    # Number of weapon equips
            "facilities_used": [],   # List of facilities interacted with
            "peak_hp": 100,          # Highest HP achieved
            "lowest_hp": 100,        # Lowest HP survived
            "total_moves": 0,        # Number of move actions
            "total_rests": 0,        # Number of rest actions
        }

    async def run(self, existing_ws=None) -> dict:
        """
        Main gameplay loop. Returns game result dict.
        Per gotchas.md: connect with X-API-Key only, no gameId/agentId params.
        
        v1.6.0: If existing_ws provided (from unified /ws/join), reuse it directly
        instead of dialing a new connection.
        """
        self._existing_ws = existing_ws
        api_key = get_api_key()
        headers = {
            "Authorization": f"mr-auth {api_key}",
            "X-API-Key": api_key,
            "X-Version": SKILL_VERSION,
        }

        self._running = True
        retry_count = 0
        max_retries = 5

        # v1.6.0: If we have a socket from unified join, use it directly
        if self._existing_ws:
            log.info("Using existing socket from unified join (v1.6.0)")
            try:
                return await self._run_with_socket(self._existing_ws)
            except websockets.exceptions.ConnectionClosed:
                log.info("Unified join socket closed, falling back to reconnect...")
                self._existing_ws = None

        while self._running and retry_count < max_retries:
            try:
                log.info("Connecting WebSocket to %s...", WS_URL)
                ws_url = WS_URL
                log.info("Handshake with key: %s...", api_key[:8])
                async with websockets.connect(
                    ws_url,
                    extra_headers=headers,
                    ping_interval=None,  # We handle our own pings
                    max_size=2**20,  # 1MB max message
                    close_timeout=10,  # v12+ compatibility
                ) as ws:
                    result = await self._run_with_socket(ws)
                    if result is not None:
                        return result
                    retry_count = 0  # Reset on successful completion

            except websockets.exceptions.ConnectionClosed as e:
                retry_count += 1
                log.warning("WebSocket closed: code=%s reason=%s (retry %d/%d)",
                            e.code, e.reason, retry_count, max_retries)
                if self._ping_task:
                    self._ping_task.cancel()
                await asyncio.sleep(min(2 ** retry_count, 30))

            except Exception as e:
                retry_count += 1
                log.error("WebSocket error: %s (retry %d/%d)", e, retry_count, max_retries)
                if self._ping_task:
                    self._ping_task.cancel()
                await asyncio.sleep(min(2 ** retry_count, 30))

        return self.game_result or {"status": "disconnected"}

    async def _run_with_socket(self, ws) -> dict | None:
        """
        Run gameplay loop with an existing WebSocket.
        Used by both unified join socket reuse and fresh connections.
        """
        self.ws = ws
        retry_count = 0  # Reset on successful connect
        log.info("[OK] WebSocket connected for game=%s", self.game_id)

        # Start ping keepalive
        self._ping_task = asyncio.create_task(self._ping_loop())

        # Message processing loop
        async for raw_msg in ws:
            try:
                msg = json.loads(raw_msg)
                if not isinstance(msg, dict):
                    log.warning("Non-dict WS message: %s", type(msg).__name__)
                    continue
                msg_type = msg.get("type", "unknown")
                log.debug("WS recv: type=%s", msg_type)
                result = await self._handle_message(msg)
                if result is not None:
                    self._running = False
                    return result
            except json.JSONDecodeError:
                log.warning("Non-JSON message: %s", raw_msg[:100])
        
        return None

    async def _handle_message(self, msg: dict) -> dict | None:
        """Process a single WebSocket message. Returns game result or None."""
        msg_type = msg.get("type", "")

        # ── agent_view ────────────────────────────────────────────────
        # Per game-loop.md: uses 'view' key for state data
        # Sent on: initial connect, game start, reconnect, vision change
        if msg_type == "agent_view":
            view = msg.get("view") or msg.get("data") or {}
            if isinstance(view, dict) and view:
                self.last_view = view
                reason = msg.get("reason", "initial")
                alive = view.get("self", {}).get("isAlive", "?")
                hp = view.get("self", {}).get("hp", "?")
                ep = view.get("self", {}).get("ep", "?")
                log.info("agent_view (reason=%s) alive=%s HP=%s EP=%s", reason, alive, hp, ep)
                # Track game stats for self-learning
                if self._game_stats["start_time"] is None:
                    self._game_stats["start_time"] = time.time()
                    # Reset stats for new game
                    self_data = view.get("self", {})
                    self._game_stats["kills"] = self_data.get("kills", 0)
                    self._game_stats["moltz"] = self_data.get("moltz", 0)
                # DEBUG: log first visible agent to confirm API fields (HP, EP, equippedWeapon)
                va = view.get("visibleAgents", [])
                if va and isinstance(va[0], dict):
                    first = va[0]
                    log.debug("VISIBLE_AGENT_SAMPLE: hp=%s ep=%s atk=%s weapon=%s",
                              first.get("hp"), first.get("ep"), first.get("atk"),
                              first.get("equippedWeapon"))
                await self._on_agent_view(view)
            else:
                log.warning("agent_view with empty/invalid view: %s", str(view)[:100])

        # ── action_result ─────────────────────────────────────────────
        # Per actions.md: canAct and cooldownRemainingMs are at TOP LEVEL
        elif msg_type == "action_result":
            success = msg.get("success", False)
            # canAct is at TOP LEVEL per actions.md, NOT inside data
            self.action_sender.can_act = msg.get("canAct", self.action_sender.can_act)
            self.action_sender.cooldown_remaining_ms = msg.get("cooldownRemainingMs", 0)

            if success:
                data = msg.get("data", {})
                action_msg = data.get("message", "") if isinstance(data, dict) else str(data)
                log.info("Action OK: %s (canAct=%s)", action_msg, msg.get("canAct"))
                # Track map usage for learning on next view
                if isinstance(data, dict) and "map" in str(action_msg).lower():
                    self._map_just_used = True
            else:
                err = msg.get("error", {})
                err_code = err.get("code", "") if isinstance(err, dict) else str(err)
                err_msg = err.get("message", "") if isinstance(err, dict) else ""
                log.warning("Action FAILED: %s — %s (canAct=%s)", err_code, err_msg, msg.get("canAct"))
                
                # Report failure to brain to avoid spamming
                if self.last_sent_action:
                    from bot.strategy.brain import track_failed_action
                    track_failed_action(
                        self.last_sent_action.get("type"),
                        self.last_sent_action.get("itemId")
                    )

        # ── can_act_changed ───────────────────────────────────────────
        # Per actions.md: canAct is at TOP LEVEL
        elif msg_type == "can_act_changed":
            self.action_sender.can_act = msg.get("canAct", True)
            self.action_sender.cooldown_remaining_ms = msg.get("cooldownRemainingMs", 0)
            log.info("can_act_changed: canAct=%s", msg.get("canAct"))
            # Re-evaluate actions with current view
            if self.last_view and msg.get("canAct"):
                await self._on_agent_view(self.last_view)

        # ── turn_advanced ─────────────────────────────────────────────
        # Per game-loop.md: "turn_advanced is a pure state snapshot for a new turn"
        # It INCLUDES full 'view' data — MUST be processed like agent_view
        elif msg_type == "turn_advanced":
            # view can be at msg.view or msg.data.view or inside msg directly
            turn_num = msg.get("turn", "?")
            view = msg.get("view")
            if not view and isinstance(msg.get("data"), dict):
                view = msg["data"].get("view")
                turn_num = msg["data"].get("turn", turn_num)

            log.info("Turn %s — processing view...", turn_num)
            if view and isinstance(view, dict):
                self.last_view = view
                await self._on_agent_view(view)
            elif self.last_view:
                # No view in message — re-evaluate with last known state
                await self._on_agent_view(self.last_view)
            else:
                log.warning("Turn advanced but no view data available")

        # ── game_ended ────────────────────────────────────────────────
        elif msg_type == "game_ended":
            log.info("=== GAME ENDED ===")
            # Record match for self-learning evolution
            try:
                end_time = time.time()
                survival_time = int(end_time - self._game_stats["start_time"]) if self._game_stats["start_time"] else 0
                data = msg.get("data", {}) if isinstance(msg.get("data", {}), dict) else {}
                result = msg.get("result", {}) if isinstance(msg.get("result", {}), dict) else {}
                self_data = data.get("self") or result.get("self") or msg.get("self") or {}
                if not isinstance(self_data, dict):
                    self_data = {}
                # Helper to get value with 0 being valid (not falsy)
                def get_val(*keys, default=None, sources=[self_data, data, result, self._game_stats]):
                    for src in sources:
                        if not isinstance(src, dict):
                            continue
                        for key in keys:
                            if key in src and src[key] is not None:
                                return src[key]
                    return default
                
                placement = get_val("placement", "finalRank", default=100)
                kills = get_val("kills", default=self._game_stats["kills"])
                damage_dealt = get_val("damageDealt", "damage_dealt", default=0)
                damage_taken = get_val("damageTaken", "damage_taken", default=0)
                moltz = get_val("moltz", default=0)
                
                # NEW: Record match with detailed analytics
                record_match(
                    placement=placement,
                    kills=kills,
                    survival_time=survival_time,
                    damage_dealt=damage_dealt,
                    damage_taken=damage_taken,
                    moltz=moltz,
                    # NEW: Detailed analytics
                    cause_of_death=self._game_stats.get("cause_of_death"),
                    time_of_death=self._game_stats.get("time_of_death"),
                    last_region_id=self._game_stats.get("last_region_id"),
                    items_used=self._game_stats.get("items_used", []),
                    heal_items_used=self._game_stats.get("heal_items_used", 0),
                    weapon_switches=self._game_stats.get("weapon_switches", 0),
                    facilities_used=self._game_stats.get("facilities_used", []),
                    peak_hp=self._game_stats.get("peak_hp", 100),
                    lowest_hp=self._game_stats.get("lowest_hp", 100),
                    total_moves=self._game_stats.get("total_moves", 0),
                    total_rests=self._game_stats.get("total_rests", 0),
                )
                log.info("🧬 MATCH RECORDED | Placement: %d | Kills: %d | DMG: %d/%d | Survived: %ds | Death: %s",
                         placement, kills, damage_dealt, damage_taken, survival_time,
                         self._game_stats.get("cause_of_death", "survived"))
            except Exception as e:
                log.warning("Failed to record match: %s", e)
            reset_game_state()  # Clear curse tracking for next game
            self.game_result = msg
            return msg

        # ── event ─────────────────────────────────────────────────────
        elif msg_type == "event":
            event_type = msg.get("eventType", msg.get("data", {}).get("eventType", ""))
            log.debug("Event: %s", event_type)

            # Combat event logging - track attacks and damage
            if event_type in ("agent_attacked", "combat", "attack", "damage_dealt"):
                data = msg.get("data", {})
                # Try to find attacker/target in data or top-level
                raw_attacker = data.get("attackerId") or msg.get("attackerId") or data.get("attackerName") or "?"
                raw_target = data.get("targetId") or msg.get("targetId") or data.get("targetName") or "?"
                damage = data.get("damage") or data.get("dmg") or msg.get("damage") or "?"
                weapon = data.get("weaponName") or data.get("weaponType") or data.get("weaponId") or "?"
                is_kill = data.get("isKill") or data.get("killed") or msg.get("isKill", False)

                # Track with raw IDs (before truncation) for accurate matching
                attacker_display = raw_attacker
                target_display = raw_target
                if len(attacker_display) > 16: attacker_display = f"ID:{raw_attacker[:8]}"
                if len(target_display) > 16: target_display = f"ID:{raw_target[:8]}"

                log.info("⚔️ COMBAT: %s → %s | DMG: %s | Weapon: %s%s", 
                         attacker_display, target_display, damage, weapon, " (KILL!)" if is_kill else "")
                
                # Track damage for self-learning stats
                try:
                    dmg_val = int(damage) if isinstance(damage, (int, float, str)) and damage != "?" else 0
                    # Check matching with full ID or truncated (first 8 chars)
                    my_id = str(self.agent_id or "")
                    my_key = str(self.dashboard_key or "")
                    atk_str = str(raw_attacker)
                    tgt_str = str(raw_target)
                    
                    is_me = (my_id and (my_id in atk_str or my_id[:8] in atk_str or atk_str in my_id)) or \
                            (my_key and (my_key in atk_str or my_key[:8] in atk_str or atk_str in my_key))
                    is_target_me = (my_id and (my_id in tgt_str or my_id[:8] in tgt_str or tgt_str in my_id)) or \
                                   (my_key and (my_key in tgt_str or my_key[:8] in tgt_str or tgt_str in my_key))
                    
                    # DEBUG: Log ID matching untuk troubleshoot
                    log.debug("COMBAT_TRACK: my_id=%s my_key=%s | atk=%s tgt=%s | is_me=%s is_target_me=%s dmg=%s",
                              my_id[:8], my_key[:8], atk_str[:20], tgt_str[:20], is_me, is_target_me, dmg_val)
                    
                    if is_me and dmg_val > 0:
                        self._game_stats["damage_dealt"] += dmg_val
                        log.info("💥 DAMAGE DEALT: %d | Total: %d", dmg_val, self._game_stats["damage_dealt"])
                    if is_target_me and dmg_val > 0:
                        self._game_stats["damage_taken"] += dmg_val
                        log.info("💔 DAMAGE TAKEN: %d | Total: %d", dmg_val, self._game_stats["damage_taken"])
                    if is_me and is_kill:
                        self._game_stats["kills"] += 1
                        log.info("🎯 KILL TRACKED! Total kills this game: %d", self._game_stats["kills"])
                except Exception as e:
                    log.debug("Failed to track combat stats: %s", e)
                
                # If everything is still ?, log raw for debugging
                if raw_attacker == "?" and raw_target == "?":
                    log.debug("DEBUG_COMBAT_RAW: %s", str(msg)[:200])

        # ── waiting ───────────────────────────────────────────────────
        elif msg_type == "waiting":
            log.info("Game is waiting for players...")

        # ── pong ──────────────────────────────────────────────────────
        elif msg_type == "pong":
            pass

        # ── error ─────────────────────────────────────────────────────
        elif msg_type == "error":
            err_msg = msg.get("message", msg.get("data", {}).get("message", str(msg)))
            log.error("Server error: %s", err_msg)

        # ── unknown ───────────────────────────────────────────────────
        else:
            log.info("Unknown WS message type=%s keys=%s",
                     msg_type, list(msg.keys()))

        return None

    async def _on_agent_view(self, view: dict):
        """Process agent_view → decide action → send if appropriate."""
        if not isinstance(view, dict):
            return

        self_data = view.get("self", {})
        if not isinstance(self_data, dict):
            return

        alive_count = view.get("aliveCount", "?")

        if not self_data.get("isAlive", True):
            log.info("[DEAD] Agent DEAD — Alive remaining: %s. Waiting for game_ended...", alive_count)
            
            # NEW: Track death analytics
            region = view.get("currentRegion", {})
            region_id = region.get("id", "unknown") if isinstance(region, dict) else "unknown"
            region_name = region.get("name", "unknown") if isinstance(region, dict) else "unknown"
            
            self._game_stats["last_region_id"] = region_id
            self._game_stats["time_of_death"] = view.get("turn", "?")
            
            # Determine cause of death
            if region.get("isDeathZone"):
                self._game_stats["cause_of_death"] = "death_zone"
            elif self._game_stats["damage_taken"] > 50 and self._game_stats["lowest_hp"] <= 0:
                self._game_stats["cause_of_death"] = "combat"
            else:
                self._game_stats["cause_of_death"] = "unknown"
            
            log.info("[DEATH_ANALYTICS] Cause: %s | Region: %s | Turn: %s | Lowest HP: %d",
                     self._game_stats["cause_of_death"], region_name,
                     self._game_stats["time_of_death"], self._game_stats["lowest_hp"])
            
            # Update dashboard with dead state (don't just return silently!)
            dk = self.dashboard_key
            dashboard_state.update_agent(dk, {
                "name": self.dashboard_name,
                "status": "dead",
                "hp": 0,
                "ep": 0,
                "maxHp": self_data.get("maxHp", 100),
                "maxEp": self_data.get("maxEp", 10),
                "alive_count": alive_count,
                "last_action": f"☠️ DEAD ({self._game_stats['cause_of_death']}) — waiting for game to end",
                "enemies": [],
                "region_items": [],
            })
            dashboard_state.add_log(
                f"☠️ Agent DEAD ({self._game_stats['cause_of_death']}) — Alive remaining: {alive_count}",
                "warning", dk
            )
            return

        # Log status
        hp = self_data.get("hp", "?")
        ep = self_data.get("ep", "?")
        region = view.get("currentRegion", {})
        region_name = region.get("name", "?") if isinstance(region, dict) else "?"
        
        # NEW: Track HP analytics
        if isinstance(hp, (int, float)):
            self._game_stats["peak_hp"] = max(self._game_stats["peak_hp"], hp)
            self._game_stats["lowest_hp"] = min(self._game_stats["lowest_hp"], hp)
        
        log.info("Status: HP=%s EP=%s Region=%s | Alive: %s", hp, ep, region_name, alive_count)
        dashboard_state.add_log(
            f"HP={hp} EP={ep} Region={region_name} | Alive: {alive_count}",
            "info", self.dashboard_key
        )

        # Feed dashboard with live game data
        inv = self_data.get("inventory", [])
        enemies = [a for a in view.get("visibleAgents", [])
                   if isinstance(a, dict) and a.get("isAlive") and a.get("id") != self_data.get("id")]

        # Region items: visibleItems entries are WRAPPED: { regionId, item: {id, name, ...} }
        # We must unwrap the .item sub-object and attach regionId to it.
        region_id = region.get("id", "") if isinstance(region, dict) else ""

        def _unwrap_items(raw_items):
            """Unwrap visibleItems: each entry is { regionId, item: {...} }.
            Returns flat list of item dicts with regionId attached."""
            result = []
            for entry in raw_items:
                if not isinstance(entry, dict):
                    continue
                inner = entry.get("item")
                if isinstance(inner, dict):
                    # Attach regionId from wrapper to the inner item
                    inner["regionId"] = entry.get("regionId", "")
                    result.append(inner)
                elif entry.get("id"):
                    # Already a flat item (legacy format)
                    result.append(entry)
            return result

        region_items = []

        # Strategy 1: currentRegion.items (some game versions embed items here)
        if isinstance(region, dict) and region.get("items"):
            region_items = _unwrap_items(region["items"])

        # Strategy 2: filter visibleItems by regionId
        if not region_items:
            all_visible = _unwrap_items(view.get("visibleItems", []))
            region_items = [i for i in all_visible
                            if i.get("regionId") == region_id]

        # Strategy 3: if regionId filter returns nothing, show ALL visible items
        if not region_items:
            all_visible = _unwrap_items(view.get("visibleItems", []))
            if all_visible:
                region_items = all_visible

        equipped = self_data.get("equippedWeapon")
        weapon_name = "fist"
        weapon_bonus = 0
        if equipped and isinstance(equipped, dict):
            weapon_name = equipped.get("typeId", "fist")
            from bot.strategy.brain import WEAPONS
            weapon_bonus = WEAPONS.get(weapon_name.lower(), {}).get("bonus", 0)


        def _item_label(i):
            """Get best display label for an item.
            Try all possible field names the API might use.
            """
            return (i.get("name")
                    or i.get("typeId")
                    or i.get("type")
                    or i.get("itemType")
                    or i.get("itemName")
                    or i.get("label")
                    or i.get("kind")
                    or str(i.get("id", "?"))[:12])

        def _item_cat(i):
            """Get item category from any available field."""
            return (i.get("category")
                    or i.get("cat")
                    or i.get("itemCategory")
                    or i.get("type")
                    or "")

        dk = self.dashboard_key
        dashboard_state.update_agent(dk, {
            "name": self.dashboard_name,
            "hp": hp, "ep": ep,
            "status": "playing",
            "maxHp": self_data.get("maxHp", 100),
            "maxEp": self_data.get("maxEp", 10),
            "atk": self_data.get("atk", 0),
            "def": self_data.get("def", 0),
            "weapon": weapon_name,
            "weapon_bonus": weapon_bonus,
            "kills": self_data.get("kills", 0),
            "region": region_name,
            "alive_count": alive_count,
            "inventory": [{"typeId": i.get("typeId","?"), "name": _item_label(i), "cat": _item_cat(i)}
                          for i in inv if isinstance(i, dict)],
            "enemies": [{
                "name": e.get("name","?"),
                "hp": e.get("hp","?"),
                "ep": e.get("ep","?"),
                "id": e.get("id",""),
                "weapon": (e.get("equippedWeapon") or {}).get("typeId","?") if isinstance(e.get("equippedWeapon"), dict) else (e.get("equippedWeapon") or "?"),
            } for e in enemies[:8]],
            "region_items": [{"typeId": i.get("typeId","?"), "name": _item_label(i), "cat": _item_cat(i)}
                             for i in region_items[:10]],
        })

# ... (rest of the code remains the same)
        if self._map_just_used:
            self._map_just_used = False
            learn_from_map(view)
            log.info("🗺️ Map knowledge updated — DZ tracking active")

        # Continuous DZ tracking from every view
        _update_dz_knowledge(view)

        # Run strategy brain
        can_act = self.action_sender.can_send_cooldown_action()
        decision = decide_action(view, can_act)

        if decision is None:
            return  # No action needed now

        action_type = decision["action"]
        action_data = decision.get("data", {})
        reason = decision.get("reason", "")

        # Check if cooldown action is allowed
        # CRITICAL: Death zone escape overrides cooldown - 1.34 HP/sec damage!
        is_dz_escape = "ESCAPE" in reason or "DEATH ZONE" in reason.upper() or "DZ" in reason.upper()
        if action_type in COOLDOWN_ACTIONS and not can_act and not is_dz_escape:
            log.debug("Cooldown active — skipping %s", action_type)
            return
        if is_dz_escape and not can_act:
            log.warning("⚠️ Forcing DZ escape despite cooldown - life threatening!")

        # Build and send per actions.md envelope spec
        payload = self.action_sender.build_action(
            action_type, action_data, reason, action_type,
        )

        await self._send(payload)
        log.info("→ %s | %s", action_type.upper(), reason)
        
        # NEW: Track action analytics
        self._track_action_analytics(action_type, action_data, reason)
        
        # Store for failure tracking
        self.last_sent_action = {
            "type": action_type,
            "itemId": action_data.get("itemId"),
            "targetId": action_data.get("targetId")
        }

        # Feed dashboard with action
        dashboard_state.update_agent(self.dashboard_key, {"last_action": f"{action_type}: {reason[:60]}"})
        dashboard_state.add_log(f"{action_type}: {reason[:80]}", "info", self.dashboard_key)
    
    def _track_action_analytics(self, action_type: str, action_data: dict, reason: str):
        """Track detailed action analytics for post-match analysis."""
        
        # Track move actions
        if action_type == "move":
            self._game_stats["total_moves"] += 1
        
        # Track rest actions
        elif action_type == "rest":
            self._game_stats["total_rests"] += 1
        
        # Track item usage (healing, weapons, etc.)
        elif action_type == "use_item":
            item_id = action_data.get("itemId", "unknown")
            # Categorize item type from reason or itemId
            item_type = "unknown"
            if any(h in reason.lower() for h in ["heal", "medkit", "bandage", "food"]):
                item_type = "healing"
                self._game_stats["heal_items_used"] += 1
            elif any(w in reason.lower() for w in ["weapon", "equip"]):
                item_type = "weapon"
            elif "map" in reason.lower():
                item_type = "map"
                log.info("[MAP_TRACKING] USE_ITEM action recorded | itemId=%s | reason=%s", item_id, reason[:60])
            
            self._game_stats["items_used"].append({
                "typeId": item_id,
                "category": item_type,
                "reason": reason[:50],
                "timestamp": time.time()
            })
        
        # Track weapon equips
        elif action_type == "equip":
            self._game_stats["weapon_switches"] += 1
        
        # Track facility usage
        elif action_type == "interact":
            facility_id = action_data.get("interactableId", "unknown")
            facility_type = "unknown"
            if "medical" in reason.lower():
                facility_type = "medical_facility"
            elif "supply" in reason.lower():
                facility_type = "supply_cache"
            elif "watchtower" in reason.lower():
                facility_type = "watchtower"
            
            self._game_stats["facilities_used"].append({
                "interactableId": facility_id,
                "type": facility_type,
                "reason": reason[:50]
            })

    async def _send(self, payload: dict):
        """Send a message through WebSocket with rate limiting."""
        if self.ws is None:
            return
        await ws_limiter.acquire()
        await self.ws.send(json.dumps(payload))
        
        # Track turn timer for cooldown actions
        action_type = payload.get("data", {}).get("type", "")
        if action_type in COOLDOWN_ACTIONS:
            now = time.time()
            dashboard_state.last_action_time = now
            dashboard_state.cooldown_end_time = now + dashboard_state.turn_duration
            log.info("⏱️  TURN START: %s cooldown for %ds", action_type, dashboard_state.turn_duration)

    async def _ping_loop(self):
        """Send ping every 15s to keep connection alive per api-summary.md."""
        try:
            while self._running:
                await asyncio.sleep(15)
                if self.ws:
                    await self._send({"type": "ping"})
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.debug("Ping loop error: %s", e)
"""
Per game-loop.md §9 Message Types:
| Type              | Key Fields                                           |
|-------------------|------------------------------------------------------|
| agent_view        | gameId, agentId, status, view, reason?               |
| turn_advanced     | turn, view                                           |
| action_result     | success, data?, error?, canAct, cooldownRemainingMs  |
| can_act_changed   | canAct: true, cooldownRemainingMs: 0                 |
| event             | eventType, ...payload                                |
| game_ended        | gameId, agentId                                      |
| waiting           | gameId, agentId, message                             |
| pong              | —                                                    |
"""
