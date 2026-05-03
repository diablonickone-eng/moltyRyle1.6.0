"""
Strategy brain — main decision engine with priority-based action selection.
Implements the game-loop.md priority chain for high win rate.

v1.5.2 changes:
- Guardians now ATTACK player agents directly (hostile combatants)
- Curse is TEMPORARILY DISABLED (no whisper Q&A flow)
- Free room: 5 guardians (reduced from 30), each drops 120 sMoltz
- connectedRegions: either full Region objects OR bare string IDs — type-check!
- pendingDeathzones: entries are {id, name} objects

Uses ALL view fields from api-summary.md:
- self: agent stats, inventory, equipped weapon
- currentRegion: terrain, weather, connections, facilities
- connectedRegions: adjacent regions (full Region object when visible, bare string ID when out-of-vision)
- visibleRegions: all regions in vision range
- visibleAgents: other agents (players + guardians — guardians are HOSTILE)
- visibleMonsters: monsters
- visibleNPCs: NPCs (flavor — safe to ignore per game-systems.md)
- visibleItems: ground items in visible regions
- pendingDeathzones: regions becoming death zones next ({id, name} entries)
- recentLogs: recent gameplay events
- recentMessages: regional/private/broadcast messages
- aliveCount: remaining alive agents
"""
from bot.utils.logger import get_logger
from bot.config import (
    AGGRESSION_LEVEL, HP_CRITICAL_THRESHOLD, HP_MODERATE_THRESHOLD,
    GUARDIAN_FARM_MIN_HP, COMBAT_MIN_EP,
)
from bot.learning.strategy_dna import get_dna
from bot.autonomous_ai import autonomous_ai

log = get_logger(__name__)

# ── Weapon stats from combat-items.md ─────────────────────────────────
WEAPONS = {
    "fist": {"bonus": 0, "range": 0},
    "dagger": {"bonus": 10, "range": 0},
    "sword": {"bonus": 20, "range": 0},
    "katana": {"bonus": 35, "range": 0},
    "bow": {"bonus": 5, "range": 1},
    "pistol": {"bonus": 10, "range": 1},
    "sniper": {"bonus": 28, "range": 2},
}

WEAPON_PRIORITY = ["katana", "sniper", "sword", "pistol", "dagger", "bow", "fist"]

# ── Item priority for pickup ──────────────────────────────────────────
# Moltz = ALWAYS pickup (highest). Weapons > healing > utility.
# Binoculars = passive (vision+1 just by holding), always pickup.
ITEM_PRIORITY = {
    "rewards": 300,  # Moltz/sMoltz — ALWAYS pickup first
    "katana": 120, "sniper": 115, "sword": 110, "pistol": 105,
    "dagger": 100, "bow": 95,
    "medkit": 70, "bandage": 65, "emergency_food": 60, "energy_drink": 58,
    "binoculars": 55,  # Passive: vision +1 permanent, always pickup
    "map": 52,         # Reveal entire map (consumable/one-time)
    "megaphone": 50,   # Global broadcast (consumable)
    "moltz": 10,       # Ground currency (pickup if nothing else)
}

# ── Recovery items for healing (combat-items.md) ──────────────────────
# For normal healing (HP<70): prefer Emergency Food (save Bandage/Medkit)
# For critical healing (HP<30): prefer Bandage then Medkit
RECOVERY_ITEMS = {
    "medkit": 50, "bandage": 30, "emergency_food": 20,
    "energy_drink": 0,  # EP restore, not HP
}

# Weather combat penalty per game-systems.md
WEATHER_COMBAT_PENALTY = {
    "clear": 0.0,
    "rain": 0.05,   # -5%
    "fog": 0.10,    # -10%
    "storm": 0.15,  # -15%
}


def calc_damage(atk: int, weapon_bonus: int, target_def: int,
                weather: str = "clear") -> int:
    """Damage formula per combat-items.md + game-systems.md weather penalty.
    Base: ATK + bonus - (DEF * 0.5), min 1.
    Weather: clear=0%, rain=-5%, fog=-10%, storm=-15%.
    """
    base = atk + weapon_bonus - int(target_def * 0.5)
    penalty = WEATHER_COMBAT_PENALTY.get(weather, 0.0)
    return max(1, int(base * (1 - penalty)))


def get_weapon_bonus(equipped_weapon) -> int:
    """Get ATK bonus from equipped weapon."""
    if not equipped_weapon:
        return 0
    type_id = equipped_weapon.get("typeId", "").lower()
    return WEAPONS.get(type_id, {}).get("bonus", 0)


def get_weapon_range(equipped_weapon) -> int:
    """Get range from equipped weapon."""
    if not equipped_weapon:
        return 0
    type_id = equipped_weapon.get("typeId", "").lower()
    return WEAPONS.get(type_id, {}).get("range", 0)

_known_agents: dict = {}
# Map knowledge: track all revealed DZ/pending DZ/safe regions after using Map
_map_knowledge: dict = {"revealed": False, "death_zones": set(), "safe_center": []}
# Exploration memory: track visited regions to avoid redundant exploration
_visited_regions: set = set()
# Guardian hunting: track last known guardian locations
_guardian_locations: dict = {}  # {region_id: last_seen_turn}
# Failed actions blacklist: {action_key: expiry_turn}
_failed_actions: dict = {}
_current_turn: int = 0

# Combat metrics tracking for performance analysis
_combat_metrics: dict = {
    "attacks_attempted": 0,
    "attacks_successful": 0,
    "kills": 0,
    "deaths": 0,
    "damage_dealt": 0,
    "damage_taken": 0,
    "finisher_kills": 0,
    "ranged_attacks": 0,
    "chase_attempts": 0,
    "combat_avoided": 0,
    "enemies_seen": 0,
    "turns_alive": 0,
}


def _resolve_region(entry, view: dict):
    """Resolve a connectedRegions entry to a full region object.
    Per v1.5.2 gotchas.md §3: entries are EITHER full Region objects
    (when adjacent region is within vision) OR bare string IDs (when out-of-vision).
    Returns the full object, or None if out-of-vision.
    """
    if isinstance(entry, dict):
        return entry  # Full object
    if isinstance(entry, str):
        # Look up in visibleRegions
        for r in view.get("visibleRegions", []):
            if isinstance(r, dict) and r.get("id") == entry:
                return r
    return None  # Out-of-vision — only ID is known


def _get_region_id(entry) -> str:
    """Extract region ID from either a string or dict entry."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return entry.get("id", "")
    return ""


def reset_game_state():
    """Reset per-game tracking state. Call when game ends."""
    global _known_agents, _map_knowledge, _visited_regions, _guardian_locations, _planned_next_action, _failed_actions, _current_turn, _combat_metrics
    # Log final metrics before reset
    _log_combat_metrics()
    _known_agents = {}
    _map_knowledge = {"revealed": False, "death_zones": set(), "safe_center": []}
    _visited_regions = set()
    _guardian_locations = {}
    _planned_next_action = None
    _failed_actions = {}
    _current_turn = 0
    # Reset metrics for new game
    _combat_metrics = {
        "attacks_attempted": 0, "attacks_successful": 0, "kills": 0, "deaths": 0,
        "damage_dealt": 0, "damage_taken": 0, "finisher_kills": 0, "ranged_attacks": 0,
        "chase_attempts": 0, "combat_avoided": 0, "enemies_seen": 0, "turns_alive": 0,
    }
    log.info("Strategy brain reset for new game")


def _log_combat_metrics():
    """Log combat performance metrics at end of game."""
    global _combat_metrics
    m = _combat_metrics
    attacks = m["attacks_attempted"]
    kills = m["kills"]
    success_rate = (m["attacks_successful"] / attacks * 100) if attacks > 0 else 0
    log.info("=" * 50)
    log.info(" COMBAT METRICS REPORT")
    log.info("=" * 50)
    log.info("Attacks Attempted: %d | Successful: %d (%.1f%%)", attacks, m["attacks_successful"], success_rate)
    log.info("Kills: %d | Finisher Kills: %d", kills, m["finisher_kills"])
    log.info("Ranged Attacks: %d | Chase Attempts: %d", m["ranged_attacks"], m["chase_attempts"])
    log.info("Combat Avoided: %d | Enemies Seen: %d", m["combat_avoided"], m["enemies_seen"])
    log.info("Damage Dealt: %d | Damage Taken: %d", m["damage_dealt"], m["damage_taken"])
    log.info("Turns Alive: %d", m["turns_alive"])
    log.info("=" * 50)


def _track_attack(attack_type: str = "melee", is_finisher: bool = False):
    """Track combat attack attempt."""
    global _combat_metrics
    _combat_metrics["attacks_attempted"] += 1
    if attack_type == "ranged":
        _combat_metrics["ranged_attacks"] += 1
    if is_finisher:
        _combat_metrics["finisher_kills"] += 1


def _track_chase():
    """Track chase attempt."""
    global _combat_metrics
    _combat_metrics["chase_attempts"] += 1


def _track_enemy_seen(count: int = 1):
    """Track enemy encounters."""
    global _combat_metrics
    _combat_metrics["enemies_seen"] += count


def track_failed_action(action_type: str, item_id: str = None):
    """Blacklist an action that failed on the server."""
    global _failed_actions
    key = action_type
    if item_id:
        key = f"{action_type}:{item_id}"
    # Blacklist for 5 turns
    _failed_actions[key] = _current_turn + 5
    log.warning("Blacklisting failed action: %s until turn %d", key, _failed_actions[key])


def decide_action(view: dict, can_act: bool, memory_temp: dict = None) -> dict | None:
    """
    Main decision engine. Returns action dict or None (wait).

    Priority chain per game-loop.md section 3 (v1.5.2):
    1. DEATHZONE ESCAPE (overrides everything - 1.34 HP/sec!)
    1b. Pre-escape pending death zone
    2. [DISABLED] Curse resolution - curse temporarily disabled in v1.5.2
    2b. Guardian threat evasion (guardians now attack players!)
    3. Critical healing
    3b. Use utility items (Map, Energy Drink)
    4. Free actions (pickup, equip)
    5. Smart Agent Combat (Prioritize players if we have good gear/resources)
    6. Guardian farming (120 sMoltz per kill)
    7. Monster farming
    8. Facility interaction
    8b. FACILITY CAMPING / PATROL (Wait for prey if HP/EP low)
    9. Strategic movement (NEVER into DZ or pending DZ)
    10. Rest

    Uses ALL api-summary.md view fields for decision making.
    """
    global _current_turn
    _current_turn += 1

    self_data = view.get("self", {})
    region = view.get("currentRegion", {})
    hp = self_data.get("hp", 100)
    ep = self_data.get("ep", 10)
    max_ep = self_data.get("maxEp", 10)
    atk = self_data.get("atk", 10)
    defense = self_data.get("def", 5)
    is_alive = self_data.get("isAlive", True)
    inventory = self_data.get("inventory", [])
    equipped = self_data.get("equippedWeapon")

    # View-level fields per api-summary.md
    visible_agents = view.get("visibleAgents", [])
    visible_monsters = view.get("visibleMonsters", [])
    visible_npcs = view.get("visibleNPCs", [])
    visible_items_raw = view.get("visibleItems", [])
    # Unwrap: each visibleItem is { regionId, item: { id, name, typeId, ... } }
    visible_items = []
    for entry in visible_items_raw:
        if not isinstance(entry, dict):
            continue
        inner = entry.get("item")
        if isinstance(inner, dict):
            inner["regionId"] = entry.get("regionId", "")
            visible_items.append(inner)
        elif entry.get("id"):
            entry["regionId"] = entry.get("regionId", "")  # Ensure regionId exists
            visible_items.append(entry)  # Legacy flat format
    # Map enemies to regions for movement scoring
    enemy_region_count = {}
    for a in visible_agents:
        if isinstance(a, dict) and a.get("isAlive") and a.get("id") != self_data.get("id"):
            rid = a.get("regionId")
            if rid:
                enemy_region_count[rid] = enemy_region_count.get(rid, 0) + 1

    # Scan loot in current region vs nearby regions
    items_here = [i for i in visible_items if i.get("regionId") == region.get("id", "")]
    items_nearby = [i for i in visible_items if i.get("regionId") != region.get("id", "") and i.get("regionId")]
    weapons_here = [i for i in items_here if i.get("category") == "weapon" or i.get("typeId", "").lower() in WEAPONS]
    healing_here = [i for i in items_here if i.get("typeId", "").lower() in RECOVERY_ITEMS]
    currency_here = [i for i in items_here if i.get("typeId", "").lower() in ("rewards", "moltz")]
    
    names_here = [i.get("typeId", i.get("name", "?")) for i in items_here]
    names_nearby = [i.get("typeId", i.get("name", "?")) for i in items_nearby]
    
    log.info("LOOT_SCAN: total_visible=%d | HERE=%s | NEARBY=%s",
             len(visible_items), names_here, names_nearby)
    
    # Inventory summary for monitoring
    inv_heals = len([i for i in inventory if i.get("typeId", "").lower() in RECOVERY_ITEMS])
    inv_wpns = len([i for i in inventory if i.get("category") == "weapon" or i.get("typeId", "").lower() in WEAPONS])
    inv_maps = len([i for i in inventory if isinstance(i, dict) and i.get("typeId", "").lower() == "map"])
    log.info("INVENTORY: HP=%d EP=%d | HealItems=%d Weapons=%d Maps=%d | WeaponEquipped=%s",
             hp, ep, inv_heals, inv_wpns, inv_maps, equipped.get("typeId") if isinstance(equipped, dict) else equipped)
    if inv_maps > 0:
        log.info("[MAP_TRACKING] Inventory contains %d Map(s) - should use immediately if not used yet", inv_maps)
    visible_regions = view.get("visibleRegions", [])
    connected_regions = view.get("connectedRegions", [])
    pending_dz = view.get("pendingDeathzones", [])
    recent_logs = view.get("recentLogs", [])
    messages = view.get("recentMessages", [])
    alive_count = view.get("aliveCount", 100)

    # Fallback connections from currentRegion if connectedRegions empty
    connections = connected_regions or region.get("connections", [])
    interactables = region.get("interactables", [])
    region_id = region.get("id", "")
    region_terrain = region.get("terrain", "").lower() if isinstance(region, dict) else ""
    region_weather = region.get("weather", "").lower() if isinstance(region, dict) else ""

    # NEW: Detect guardians from whisper messages (they whisper from their location)
    _detect_guardians_from_whispers(messages, region_id, connected_regions, visible_regions)

    # Combat/Aggression pre-calculations
    enemies = [a for a in visible_agents if not a.get("isGuardian") and a.get("isAlive") and a.get("id") != self_data.get("id")]
    w_type = equipped.get("typeId", "").lower() if isinstance(equipped, dict) else ""
    has_weapon = w_type in ("katana", "sniper", "sword", "pistol", "dagger", "bow")
    healing_count = len([i for i in inventory if i.get("typeId", "").lower() in RECOVERY_ITEMS])
    w_range = WEAPONS.get(w_type, {}).get("range", 0)

    # enemies_here: agents in the same region OR agents with no regionId (API may omit it)
    # Per api-summary.md, visibleAgents does NOT guarantee a regionId field.
    # If regionId missing → assume same region (they're in our vision = likely co-located).
    # For ranged weapons, also include adjacent-region enemies.
    enemies_here = [
        e for e in enemies
        if not e.get("regionId")              # No regionId → assume same region
        or e.get("regionId") == region_id     # Explicitly same region
    ]
    # For ranged weapons, also consider adjacent-region enemies as attackable
    if w_range >= 1:
        adjacent_ids = set(_get_region_id(c) for c in (connected_regions or region.get("connections", [])))
        # Normalize matching: some APIs return truncated regionId (first 8 chars)
        adjacent_prefixes = {rid[:8] for rid in adjacent_ids if rid}
        enemies_in_range = [
            e for e in enemies
            if e.get("regionId") and (
                e.get("regionId") in adjacent_ids  # Full match
                or e.get("regionId")[:8] in adjacent_prefixes  # Prefix match
            )
        ]
        # FALLBACK: If no enemies have regionId at all, assume they're in adjacent for ranged
        # This handles API inconsistency where visibleAgents lack regionId field
        if not enemies_in_range and enemies and not any(e.get("regionId") for e in enemies):
            log.warning("API_FALLBACK: No enemies have regionId, assuming all in adjacent for ranged combat")
            enemies_in_range = enemies[:]  # Assume all visible enemies are in adjacent
        
        # DEBUG: Log why enemies_in_range might be 0
        log.info("RANGE_DEBUG: w_range=%d | adjacent_ids=%s | enemies_with_regionId=%s | matched=%d",
                 w_range, list(adjacent_ids)[:5], 
                 [e.get("regionId", "NO_REGION")[:8] for e in enemies],
                 len(enemies_in_range))
    else:
        enemies_in_range = []

    # ── SELF-LEARNING: Load evolved DNA parameters ──────────────
    dna = get_dna()
    game_phase = "early" if alive_count > 70 else ("mid" if alive_count > 25 else "late")
    strategy = dna.get_strategy_params(game_phase, hp, alive_count)
    
    log.info("🧬 DNA_STRATEGY: phase=%s | aggression=%.2f | finisher_threshold=%d | war_ready_hp=%d",
             game_phase, strategy["aggression"], strategy["finisher_threshold"], 
             strategy["ready_for_war_hp"])
    
    # Aggression criteria: weapon + at least 1 healing item + decent HP
    # Using LEARNED DNA parameters instead of static values!
    is_ready_for_war = has_weapon and healing_count >= 1 and hp >= strategy["ready_for_war_hp"]
    # FINISHER logic: If enemy is weak, we don't need "ready for war"
    # Using LEARNED finisher threshold
    finisher_threshold = strategy["finisher_threshold"]
    finisher_targets = [e for e in enemies if e.get("hp", 100) < finisher_threshold]

    # Log enemy scan for debugging — critical to trace why attack isn't firing
    log.info("ENEMY_SCAN: total_visible=%d | here=%d | in_range=%d | finishers=%d | ready_for_war=%s | w_type=%s",
             len(enemies), len(enemies_here), len(enemies_in_range), len(finisher_targets),
             is_ready_for_war, w_type or "fist")

    # Survival gate: TIME EFFICIENT - prioritize kills over survival
    # Mode-based minimum HP for combat - ULTRA AGGRESSIVE for maximum kills
    aggression = AGGRESSION_LEVEL.lower() if AGGRESSION_LEVEL else "balanced"
    
    # TIME EFFICIENT THRESHOLDS: Lower HP for more combat opportunities
    if aggression == "aggressive":
        mode_hp_min = 15  # Ultra aggressive: engage at 15 HP
    elif aggression == "passive":
        mode_hp_min = 35  # Still more aggressive than before
    else:
        mode_hp_min = 25  # Balanced: engage at 25 HP
    
    # Phase-based floors: even more aggressive for time efficiency
    if alive_count <= 10:  # Endgame - desperation mode
        phase_floor = 10
    elif alive_count <= 25:  # Late game - aggressive
        phase_floor = 15
    else:  # Early game - maximum aggression
        phase_floor = 20
    
    combat_hp_floor = max(strategy["combat_hp_threshold"], phase_floor, mode_hp_min)
    can_afford_combat = hp >= combat_hp_floor or (
        hp >= _get_combat_hp_threshold(alive_count, equipped) and is_ready_for_war
    )

    if not is_alive:
        return None  # Dead — wait for game_ended

    # Log current region state for debugging
    fac_types = [f.get("type",f.get("typeId","?")) for f in interactables if isinstance(f, dict)]
    enemies_here_names = [e.get("name", e.get("id","?")[:8]) for e in enemies_here]
    log.info("COMBAT_GATE: hp=%d floor=%d phase_floor=%d can_afford=%s",
             hp, combat_hp_floor, phase_floor, can_afford_combat)
    log.info("REGION_STATE: %s (%s) | terrain=%s | weather=%s | interactables=%s | enemies_here=%s",
             region.get("name", "Unknown"),
             region_id[:8] if len(str(region_id)) > 8 else region_id,
             region_terrain, region_weather, fac_types, enemies_here_names)

    # ── Build FULL danger map (DZ + pending DZ) ───────────────────
    # Used by ALL movement decisions to NEVER move into danger.
    # v1.5.2: pendingDeathzones entries are {id, name} objects
    danger_ids = set()
    for dz in pending_dz:
        if isinstance(dz, dict):
            danger_ids.add(dz.get("id", ""))
        elif isinstance(dz, str):
            danger_ids.add(dz)  # Legacy fallback
    # Also mark currently-active death zones from connected regions
    for conn in connections:
        resolved = _resolve_region(conn, view)
        if resolved and resolved.get("isDeathZone"):
            danger_ids.add(resolved.get("id", ""))

    # Track visible agents for memory
    _track_agents(visible_agents, self_data.get("id", ""), region_id)
    
    # Track guardian locations for hunting
    _track_guardians(visible_agents, region_id)
    
    # Mark current region as visited
    global _visited_regions
    _visited_regions.add(region_id)

    # ── Priority 1: DEATHZONE ESCAPE (overrides everything) ───────
    # Per game-systems.md: 1.34 HP/sec damage — bot dies fast!
    move_ep_cost = _get_move_ep_cost(region_terrain, region_weather)
    if region.get("isDeathZone", False):
        safe = _find_safe_region(connections, danger_ids, view)
        if safe and ep >= move_ep_cost:
            log.warning("🚨 IN DEATH ZONE! Escaping to %s (HP=%d)", safe, hp)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": f"ESCAPE: In death zone! HP={hp} dropping fast (1.34/sec)"}
        elif not safe:
            log.error("🚨 IN DEATH ZONE but NO SAFE REGION! All neighbors are DZ!")

    # ── Priority 1b: Pre-escape pending death zone ────────────────
    if region_id in danger_ids:
        safe = _find_safe_region(connections, danger_ids, view)
        if safe and ep >= move_ep_cost:
            log.warning("⚠️ Region %s becoming DZ soon! Escaping to %s", region_id[:8], safe)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": "PRE-ESCAPE: Region becoming death zone soon"}

    # ── Priority 2: Curse resolution — DISABLED in v1.5.2 ─────────
    # Curse is temporarily disabled. Guardians no longer curse players.
    # Legacy code kept inert — will re-enable when curse returns.
    # (was: _check_curse → whisper answer to guardian)

    # ── Priority 2b: Threat evasion (guardians + strong enemies + OUTNUMBERED) ───
    # Outnumbered thresholds per mode (aggression sudah didefine di atas)
    if aggression == "aggressive":
        outnumbered_threshold = 4 if has_weapon else 3  # Combat: tahan 3-4 musuh
    elif aggression == "passive":
        outnumbered_threshold = 2 if has_weapon else 1  # Survive: kabur dari 1-2 musuh
    else:  # balanced
        outnumbered_threshold = 3 if has_weapon else 2
    
    # DETEKSI CLAN/PARTY: 2 metode - name pattern + behavior
    # Method 1: Name pattern (prefix/suffix sama)
    def _extract_clan_tag(name):
        if not name or '_' not in name:
            return None
        return name.split('_')[0] if '_' in name else None
    
    clan_counts = {}
    for enemy in visible_agents:
        if enemy.get("isAlive") and not enemy.get("isGuardian"):
            clan = _extract_clan_tag(enemy.get("name", ""))
            if clan:
                clan_counts[clan] = clan_counts.get(clan, 0) + 1
    
    dominant_clan = max(clan_counts, key=clan_counts.get) if clan_counts else None
    dominant_count = clan_counts.get(dominant_clan, 0) if dominant_clan else 0
    
    # Method 2: Behavior-based (high concentration = suspected party)
    # Jika 4+ musuh di region sama, anggap potential party (even beda nama)
    enemy_count = len(enemies_here)
    suspected_party = (enemy_count >= 4) or (dominant_count >= 3 and dominant_clan)
    
    if suspected_party:
        # Only flee from clan parties if they're significantly larger (4+ players) OR we're low HP
        clan_flee_threshold = 4 if hp >= 70 else 3
        if dominant_count >= clan_flee_threshold and dominant_clan:
            log.warning("🚨 CLAN/PARTY DETECTED: '%s_' x%d enemies! Coordinated group!", 
                       dominant_clan, dominant_count)
            reason = f"CLAN FLEE: Coordinated group '{dominant_clan}_' x{dominant_count}!"
        else:
            log.info("👥 Clan party '%s_' x%d detected but manageable (HP=%d)", 
                    dominant_clan or "unknown", dominant_count, hp)
            suspected_party = False  # Don't flee - fight or observe
        
        # Force flee regardless of mode - 1 vs party = suicide
        safe = _find_safe_region_with_exit(connections, danger_ids, view)
        if safe and ep >= move_ep_cost:
            return {"action": "move", "data": {"regionId": safe}, "reason": reason}
    
    guardians_here = [a for a in visible_agents
                      if a.get("isGuardian", False) and a.get("isAlive", True)
                      and a.get("regionId") == region_id]
    # enemies_here sudah didefinisikan di atas dengan benar (termasuk yang tanpa regionId)

    # EMERGENCY: Flee if OUTNUMBERED (mode-dependent)
    enemy_count = len(enemies_here)
    if enemy_count >= outnumbered_threshold and ep >= move_ep_cost:
        safe = _find_safe_region_with_exit(connections, danger_ids, view)
        if safe:
            log.warning("🚨 OUTNUMBERED! %d enemies vs 1, mode=%s, threshold=%d — FLEEING!", 
                       enemy_count, aggression, outnumbered_threshold)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": f"OUTNUMBERED FLEE: {enemy_count} enemies vs 1 (mode={aggression})"}
    
    # AGGRESSIVE CHASE: In combat mode, pursue enemies even if slightly risky
    if aggression == "aggressive" and enemies_here and has_weapon and hp >= 40:
        # Cari musuh terdekat yang bisa dikejar
        for enemy in enemies_here:
            enemy_rid = enemy.get("regionId")
            if enemy_rid and enemy_rid != region_id:
                if any(_get_region_id(c) == enemy_rid for c in connections):
                    if enemy_rid not in danger_ids:
                        log.info("⚔️ AGGRESSIVE_CHASE: Pursuing %s to %s (HP=%d)", 
                                 enemy.get('name','?'), enemy_rid[:8], enemy.get('hp', '?'))
                        return {"action": "move", "data": {"regionId": enemy_rid},
                                "reason": f"AGGRESSIVE CHASE: Hunting {enemy.get('name','?')}"}

    # Flee from guardians when HP low (with retreat path planning)
    if guardians_here and hp < GUARDIAN_FARM_MIN_HP and ep >= move_ep_cost:
        safe = _find_safe_region_with_exit(connections, danger_ids, view)
        if safe:
            log.warning("⚠️ Guardian threat! HP=%d, fleeing", hp)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": f"GUARDIAN FLEE: HP={hp}, too dangerous"}

    # Flee from strong enemies (they deal more damage than us) with retreat path planning
    # Mode-based HP threshold: aggressive=40, balanced=50, passive=60
    strong_enemy_hp_threshold = 40 if aggression == "aggressive" else (60 if aggression == "passive" else 50)
    if enemies_here and hp < strong_enemy_hp_threshold and ep >= move_ep_cost:
        my_bonus = get_weapon_bonus(equipped)
        for enemy in enemies_here:
            e_dmg = calc_damage(enemy.get("atk", 10), _estimate_enemy_weapon_bonus(enemy), defense, region_weather)
            my_dmg = calc_damage(atk, my_bonus, enemy.get("def", 5), region_weather)
            if e_dmg > my_dmg * 1.3 and hp < e_dmg * 3:  # Enemy hits harder + we die in ~3 hits
                safe = _find_safe_region_with_exit(connections, danger_ids, view)
                if safe:
                    log.warning("⚠️ Outmatched! Enemy dmg=%d vs ours=%d, fleeing", e_dmg, my_dmg)
                    return {"action": "move", "data": {"regionId": safe},
                        "reason": f"OUTMATCHED FLEE: Enemy dmg={e_dmg} vs {my_dmg}, HP={hp}"}

    # ── Priority 4b: DEFENSIVE ATTACK (Musuh di region sama = bahaya!) ─────────────────
    # TIME EFFICIENT: Attack more frequently for maximum kills in 59 turns
    # Lower EP threshold for more combat opportunities
    MIN_EP_FOR_COMBAT = max(1, COMBAT_MIN_EP - 1)  # Allow combat with minimal EP
    if enemies_here and has_weapon and hp >= 20 and ep >= MIN_EP_FOR_COMBAT:
        target = _select_weakest(enemies_here)
        if target:
            # IMPROVED: Use comprehensive enemy strength estimation
            enemy_strength = _estimate_enemy_strength(target)
            enemy_hp = enemy_strength["hp"]
            enemy_name = target.get("name", "?")
            
            # Calculate damage using comprehensive stats
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped), enemy_strength["def"], region_weather)
            enemy_dmg = calc_damage(enemy_strength["atk"], enemy_strength["weapon_bonus"], defense, region_weather)
            
            # IMPROVED: Consider effective HP (HP + heal potential) instead of raw HP
            enemy_effective_hp = enemy_strength["effective_hp"]
            threat_level = enemy_strength["threat_level"]
            
            # ONLY attack if we have clear advantage or can one-shot
            can_one_shot = enemy_effective_hp <= my_dmg  # Can kill even with heals
            we_stronger = my_dmg > enemy_dmg * 1.2 and hp >= enemy_effective_hp * 0.8
            is_finisher = enemy_hp < finisher_threshold  # Use raw HP for finisher check
            
            # NEW: Aggressive advantage exploitation - weapon vs fists is always worth attacking
            has_weapon_advantage = has_weapon and enemy_strength["weapon_type"] == "fist"
            huge_damage_advantage = my_dmg >= enemy_dmg * 2  # Double damage is huge advantage
            
            # IMPROVED: Consider EP advantage - if enemy has high EP, they can act more
            ep_advantage = ep >= enemy_strength["ep"]
            
            log.info("🎯 ENEMY_ANALYSIS: %s | Threat=%d | EffectiveHP=%d | DMG=%d | EP=%d | Heals=%d | Wpn=%s",
                     enemy_name, threat_level, enemy_effective_hp, enemy_dmg, 
                     enemy_strength["ep"], enemy_strength["estimated_heals"], enemy_strength["weapon_type"])
            
            # AGGRESSIVE: Attack with any advantage + weapon vs fists
            should_attack = (can_one_shot or we_stronger or is_finisher or 
                           has_weapon_advantage or  # ANY weapon vs fists is worth attacking
                           (has_weapon_advantage and huge_damage_advantage))
            
            if should_attack and ep_advantage:
                attack_reason = "AGGRESSIVE" if has_weapon_advantage else "DEFENSIVE"
                log.info("⚔️ %s_ATTACK: %s in same region (Threat=%d, WeaponAdv=%s) — attacking!", 
                         attack_reason, enemy_name, threat_level, has_weapon_advantage)
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"{attack_reason}: Attacking {enemy_name} (threat={threat_level}, effHP={enemy_effective_hp})"}
            else:
                # ENHANCED FLEE LOGIC: Consider EP disadvantage and weapon advantage
                reason_detail = []
                should_flee = True
                
                if not can_one_shot and not we_stronger and not is_finisher:
                    reason_detail.append("stronger")
                    should_flee = True
                
                # EP DISADVANTAGE: Enemy with low EP is less dangerous
                if enemy_strength["ep"] <= 2:  # Enemy EP <= 2 = not much threat
                    reason_detail.append("enemy_low_EP")
                    should_flee = False  # Don't flee from low EP enemies
                
                # WEAPON ADVANTAGE: Don't flee if we have range advantage
                if w_range >= 1 and enemy_strength["weapon_range"] == 0:  # We have ranged, enemy melee
                    reason_detail.append("our_range_advantage")
                    should_flee = False  # Don't flee from melee enemies with ranged weapon
                
                if should_flee:
                    log.warning("🚨 STRONGER ENEMY! %s (Threat=%d, EffHP=%d, DMG=%d, EP=%d) > us — FLEE!",
                               enemy_name, threat_level, enemy_effective_hp, enemy_dmg, enemy_strength["ep"])
                    safe = _find_safe_region_with_exit(connections, danger_ids, view)
                    if safe and ep >= move_ep_cost:
                        return {"action": "move", "data": {"regionId": safe},
                                "reason": f"FLEE: {enemy_name} threat={threat_level} ({', '.join(reason_detail)})"}
                else:
                    log.info("🛡️ NO_FLEE: Enemy strong but我们有 advantages (EP=%d, Range=%d) - STANDING GROUND!", 
                             enemy_strength["ep"], w_range)
                    # Don't flee - stand ground and fight

    # ── Priority 5: Free actions (pickup, equip) ─────────────────
    # COMBAT PRIORITY: Only do free actions if NO enemies nearby!
    # If enemies in range, combat takes priority over inventory management
    if not enemies_here and not enemies_in_range:  # Safe to loot/interact
        # Moderate healing in safe area
        heal_action = _moderate_heal_in_safe_area(hp, inventory, healing_count, enemies_here, region_id)
        if heal_action:
            return heal_action
        
        # Auto-pickup Moltz (currency) and valuable items
        pickup_action = _check_pickup(visible_items, inventory, region_id)
        if pickup_action:
            return pickup_action

    # Auto-equip better weapon (higher priority than pickup)
    equip_action = _check_equip(inventory, equipped)
    if equip_action:
        return equip_action
    
    # Use utility items: Map (reveal map), Megaphone (broadcast)
    util_action = _use_utility_item(inventory, hp, ep, alive_count)
    if util_action:
        return util_action
    
    # COMBAT WARNING: Skip inventory management when enemies nearby
    if enemies_here or enemies_in_range:
        log.info("🚨 COMBAT_PRIORITY: Enemies nearby (%d here, %d in range) - skipping inventory management", 
                 len(enemies_here), len(enemies_in_range))

    # ── Priority 2: IMMEDIATE COMBAT (Before cooldown check!) ─────────
    # COMBAT URGENT: Attack enemies in range even during cooldown if possible
    # This prevents missing kill opportunities due to cooldown timing
    weather_ok = region_weather not in ("storm", "fog") or w_range >= 1
    
    if enemies_in_range and w_range >= 1 and ep >= COMBAT_MIN_EP and weather_ok:
        log.info("🎯 URGENT_COMBAT: %d enemies in range - attempting attack!", len(enemies_in_range))
        
        # Try to attack even during cooldown - game will reject if not allowed
        weakest = _select_weakest(enemies_in_range)
        if weakest:
            log.info("🏹 URGENT_RANGED_ATTACK: Targeting weakest %s (HP=%s)",
                     weakest.get("name", "?"), weakest.get("hp", "?"))
            _track_attack(attack_type="ranged")
            return {"action": "attack",
                    "data": {"targetId": weakest["id"], "targetType": "agent"},
                    "reason": f"URGENT_COMBAT: Attacking {weakest.get('name','?')} "
                              f"(HP={weakest.get('hp','?')} W={w_type})"}
    
    # Same region enemies - highest priority
    if enemies_here and ep >= COMBAT_MIN_EP and weather_ok:
        log.info("🎯 URGENT_SAME_REGION: %d enemies in same region - attacking!", len(enemies_here))
        weakest = _select_weakest(enemies_here)
        if weakest:
            log.info("⚔️ URGENT_MELEE_ATTACK: Targeting weakest %s (HP=%s)",
                     weakest.get("name", "?"), weakest.get("hp", "?"))
            _track_attack(attack_type="melee")
            return {"action": "attack",
                    "data": {"targetId": weakest["id"], "targetType": "agent"},
                    "reason": f"URGENT_COMBAT: Attacking {weakest.get('name','?')} "
                              f"(HP={weakest.get('hp','?')} in same region"}

    # If cooldown active, only free actions allowed
    if not can_act:
        return None

    # ── Priority 3: Critical healing ─────────────────────────────
    # CRITICAL: If HP is low, healing is the ONLY priority.
    if hp < HP_CRITICAL_THRESHOLD:
        heal = _find_healing_item(inventory, critical=True)
        if heal:
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"CRITICAL HEAL: HP={hp}<{HP_CRITICAL_THRESHOLD}, using {heal.get('typeId', 'heal')}"}
        
        # EMERGENCY FLEE: If no healing items and HP is very low, RUN AWAY!
        if hp < 25 and connections and enemies_here:
            safe_conns = [c for c in connections if _get_region_id(c) not in danger_ids]
            if safe_conns:
                # Prefer regions without enemies
                best_escape = safe_conns[0]
                for c in safe_conns:
                    if enemy_region_count.get(_get_region_id(c), 0) == 0:
                        best_escape = c
                        break
                rid = _get_region_id(best_escape)
                log.warning("🚨 EMERGENCY FLEE: HP=%d and NO HEALS! Running to %s", hp, rid[:8])
                _track_chase()
                return {"action": "move", "data": {"regionId": rid},
                        "reason": f"ESCAPE: Low HP ({hp}) and no healing items!"}
    
    # ── Priority 4: Kill Finisher (Attack before Looting!) ─────────
    # If there's a weak enemy in the SAME region, KILL them before they move or heal.
    if finisher_targets and ep >= COMBAT_MIN_EP and can_afford_combat:
        # Same logic as enemies_here: include enemies without regionId (assume same region)
        targets_here = [e for e in finisher_targets if not e.get("regionId") or e.get("regionId") == region_id]
        if targets_here:
            target = _select_weakest(targets_here)
            if target:
                _track_attack(attack_type="melee", is_finisher=True)
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"FINISHER: Killing weak target {target.get('name','?')} (HP={target.get('hp')}) before looting"}
        
        # RANGED FINISHER: Kill weak enemies in adjacent regions (Bow/Pistol/Sniper)
        if w_range >= 1 and enemies_in_range:
            finishers_in_range = [e for e in finisher_targets if e in enemies_in_range]
            if finishers_in_range:
                target = _select_weakest(finishers_in_range)
                if target:
                    log.info("🏹 RANGED_FINISHER: Killing weak %s in adjacent region (HP=%s)",
                             target.get("name", "?"), target.get("hp", "?"))
                    _track_attack(attack_type="ranged", is_finisher=True)
                    return {"action": "attack",
                            "data": {"targetId": target["id"], "targetType": "agent"},
                            "reason": f"RANGED FINISHER: Killing weak {target.get('name','?')} (HP={target.get('hp')}) with {w_type}"}

    # ── Priority 5: Free actions (pickup, equip) ─────────────────
    # Moderate healing in safe area
    elif hp < HP_MODERATE_THRESHOLD and not enemies_here:
        heal = _find_healing_item(inventory, critical=False)
        if heal:
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"HEAL: HP={hp}, area safe, using {heal.get('typeId', 'heal')}"}

    # ── EP Conservation: save EP for DZ escape if pending DZ nearby ─
    dz_threatening = region_id in danger_ids or any(
        _get_region_id(c) in danger_ids for c in connections
    )
    ep_reserve = move_ep_cost if dz_threatening else 0

    # ── Priority 6: EP recovery if EP low ─────────────────────────
    # Energy drink (+5 EP) > rest (+1-2 EP). Use before falling back to rest.
    # IMPROVED: Trigger earlier (EP <= 5) to prevent exhaustion
    if ep <= 5:
        energy_drink = _find_energy_drink(inventory)
        if energy_drink:
            log.info("EP_CONSERVE: EP=%d low, using energy drink (+5 EP)", ep)
            return {"action": "use_item", "data": {"itemId": energy_drink["id"]},
                    "reason": f"EP CONSERVE: EP={ep} low, using energy drink (+5 EP)"}
        # No energy drink — force rest to recover EP
        if not enemies_here and not region.get("isDeathZone"):
            log.info("EP_CONSERVE: EP=%d critically low, forcing rest (+1-2 EP)", ep)
            return {"action": "rest", "data": {},
                    "reason": f"EP CONSERVE: EP={ep} critically low, resting to recover"}

    # ── Priority 7: Smart Agent Combat (Kill Hunting) ──────────────
    # "Predator Cerdas" logic: Only hunt if we can afford it
    weather_ok = region_weather not in ("storm", "fog") or w_range >= 1
    ep_budget = COMBAT_MIN_EP + move_ep_cost + ep_reserve

    # FAST PATH A: Enemies in SAME region (or no regionId) — attack immediately!
    # FORCE SAME REGION COMBAT: Always attack enemies in same region with sniper
    if enemies_here and ep >= COMBAT_MIN_EP and can_afford_combat and weather_ok:
        log.info("🎯 SAME_REGION_FORCE_COMBAT: %d enemies in same region - ATTACKING!", len(enemies_here))
        
        # PRIORITY: Attack weakest enemy first for quick kills
        weakest = _select_weakest(enemies_here)
        if weakest:
            log.info("⚔️ SAME_REGION_ATTACK: Targeting weakest %s (HP=%s) with sniper advantage",
                     weakest.get("name", "?"), weakest.get("hp", "?"))
            _track_attack(attack_type="melee")
            return {"action": "attack",
                    "data": {"targetId": weakest["id"], "targetType": "agent"},
                    "reason": f"SAME_REGION_FORCE: Attacking {weakest.get('name','?')} "
                              f"(HP={weakest.get('hp','?')} with sniper advantage"}
        
        # Fallback: Use best target selection
        target = _select_best_target(
            enemies_here, atk, equipped, defense, region_weather,
            my_hp=hp, alive_count=alive_count
        )
        if target:
            log.info("⚔️ SAME_REGION_ATTACK: %d enemies here — targeting %s (HP=%s)",
                     len(enemies_here), target["agent"].get("name", "?"), target["agent"].get("hp", "?"))
            _track_attack(attack_type="melee")
            return {"action": "attack",
                    "data": {"targetId": target["agent"]["id"], "targetType": "agent"},
                    "reason": f"PREDATOR: Attacking {target['agent'].get('name','?')} "
                              f"(HP={target['agent'].get('hp','?')} Weapon={w_type or 'fist'})"}

    # FAST PATH B: Ranged weapon — attack adjacent-region enemies without moving!
    # FORCE COMBAT: If enemies in range, ALWAYS attack before movement
    if enemies_in_range and w_range >= 1 and ep >= COMBAT_MIN_EP and can_afford_combat:
        log.info("🎯 FORCE_COMBAT: %d enemies in range with sniper - ATTACKING!", len(enemies_in_range))
        log.debug("FAST_PATH_B: Checking %d enemies in range | ep=%d | can_afford=%s", 
                  len(enemies_in_range), ep, can_afford_combat)
        
        # PRIORITY: Attack weakest enemy first for quick kills
        weakest = _select_weakest(enemies_in_range)
        if weakest:
            log.info("🏹 RANGED_ATTACK: Targeting weakest %s in adjacent region (HP=%s)",
                     weakest.get("name", "?"), weakest.get("hp", "?"))
            _track_attack(attack_type="ranged")
            return {"action": "attack",
                    "data": {"targetId": weakest["id"], "targetType": "agent"},
                    "reason": f"RANGED: Shooting weakest {weakest.get('name','?')} "
                              f"(HP={weakest.get('hp','?')} W={w_type})"}
        
        # Fallback: Use best target selection
        target = _select_best_target(
            enemies_in_range, atk, equipped, defense, region_weather,
            my_hp=hp, alive_count=alive_count
        )
        if target:
            log.info("🏹 RANGED_ATTACK: Targeting %s in adjacent region (HP=%s)",
                     target["agent"].get("name", "?"), target["agent"].get("hp", "?"))
            _track_attack(attack_type="ranged")
            return {"action": "attack",
                    "data": {"targetId": target["agent"]["id"], "targetType": "agent"},
                    "reason": f"RANGED: Shooting {target['agent'].get('name','?')} "
                              f"(HP={target['agent'].get('hp','?')} W={w_type})"}
        else:
            # RANGED PRIORITY: Attack any enemy due to range advantage
            log.info("🏹 RANGED_PRIORITY: No 'acceptable' target, but attacking any due to range advantage")
            any_enemy = enemies_in_range[0]  # Just attack first one
            _track_attack(attack_type="ranged")
            return {"action": "attack",
                    "data": {"targetId": any_enemy["id"], "targetType": "agent"},
                    "reason": f"RANGED_PRIORITY: Attacking {any_enemy.get('name','?')} (HP={any_enemy.get('hp','?')}) with {w_type} range advantage"}

    # PATH C: General scan — target any visible enemy if in range
    if enemies and ep >= ep_budget and can_afford_combat and weather_ok:
        target = _select_best_target(
            enemies, atk, equipped, defense, region_weather,
            my_hp=hp, alive_count=alive_count
        )
        if target:
            in_same_region = target["agent"].get("regionId") == region_id
            should_kite = target.get("should_kite", False)

            # RANGED KITE: If we have range and enemy is too close, MOVE away first
            if w_range >= 1 and in_same_region and connections and should_kite:
                safe_conns = [c for c in connections if _get_region_id(c) not in danger_ids]
                if safe_conns:
                    best_escape = safe_conns[0]
                    for c in safe_conns:
                        if enemy_region_count.get(_get_region_id(c), 0) == 0:
                            best_escape = c
                            break
                    rid = _get_region_id(best_escape)
                    return {"action": "move", "data": {"regionId": rid},
                            "reason": f"KITE: Repositioning for {w_type} range advantage"}

            # Standard attack if in range
            if _is_in_range(target["agent"], region_id, w_range, connections):
                return {"action": "attack",
                        "data": {"targetId": target["agent"]["id"], "targetType": "agent"},
                        "reason": f"PREDATOR: Hunting {target['agent'].get('name','?')} "
                                  f"(W={w_type} Heal={healing_count})"}
            
            # CHASE MODE: Only for MELEE weapons!
            # Ranged weapons should NOT chase - they can shoot from adjacent
            enemy_rid = target["agent"].get("regionId")
            enemy_hp = target["agent"].get("hp", 100)
            
            # RANGED: Don't chase, just shoot from here!
            if w_range >= 1 and enemy_rid and enemy_rid != region_id:
                # Enemy in adjacent region = we can shoot them!
                log.info("🏹 RANGED_HOLD: Shooting %s (HP=%d) from safe distance", 
                         target['agent'].get('name','?'), enemy_hp)
                return {"action": "attack",
                        "data": {"targetId": target["agent"]["id"], "targetType": "agent"},
                        "reason": f"RANGED: Shooting {target['agent'].get('name','?')} from adjacent region"}
            
            # MELEE CHASE: Only if we have melee weapon and enemy is weak
            if w_range == 0:
                should_chase = (enemy_hp < 50 or  # Weak enemy
                               (is_ready_for_war and enemy_hp < 70) or  # We are strong
                               target.get("one_shot", False))  # Can one-shot
                if should_chase and enemy_rid and enemy_rid != region_id:
                    if any(_get_region_id(c) == enemy_rid for c in connections):
                        log.info("🏃 MELEE_CHASE: Pursuing %s (HP=%d) to %s", 
                                 target['agent'].get('name','?'), enemy_hp, enemy_rid[:8])
                        _track_chase()
                        return {"action": "move", "data": {"regionId": enemy_rid},
                                "reason": f"CHASE: Hunting weak {target['agent'].get('name','?')} (HP={enemy_hp})"}

    # ── AGGRESSIVE CHASE: Only for MELEE! Ranged shoots from distance ──
    # Kalau ada musuh lemah di adjacent region
    if finisher_targets and ep >= move_ep_cost and hp >= 30:
        for target in finisher_targets:
            target_rid = target.get("regionId")
            if target_rid and target_rid != region_id:
                if any(_get_region_id(c) == target_rid for c in connections):
                    if target_rid not in danger_ids:
                        # RANGED: Shoot finisher dari tempat, jangan chase!
                        if w_range >= 1:
                            log.info("� RANGED_FINISHER_SHOT: Shooting weak %s (HP=%d) from adjacent",
                                     target.get('name','?'), target.get('hp', '?'))
                            return {"action": "attack",
                                    "data": {"targetId": target["id"], "targetType": "agent"},
                                    "reason": f"RANGED FINISHER: Shooting weak {target.get('name','?')} from safe distance"}
                        # MELEE: Chase untuk finisher
                        elif w_range == 0:
                            log.info("🏃 MELEE_FINISHER_CHASE: Pursuing %s (HP=%d) to %s",
                                     target.get('name','?'), target.get('hp', '?'), target_rid[:8])
                            _track_chase()
                            return {"action": "move", "data": {"regionId": target_rid},
                                    "reason": f"FINISHER CHASE: Pursuing weak {target.get('name','?')} (HP={target.get('hp')})"}
                
    # ── Priority 7.5: SNIPER GUARDIAN HUNTER (Range 2 positioning) ─────
    # With Sniper: Find optimal range 2 position near guardians, kite low HP enemies
    is_sniper_mode = (w_type == "sniper" and w_range == 2)
    if is_sniper_mode:
        # Check if guardians known in adjacent regions
        guardian_nearby_regions = [rid for rid in _guardian_locations.keys() 
                                    if rid != region_id and rid not in danger_ids]
        
        if guardian_nearby_regions:
            # SNIPER STRATEGY: Position at range 2 from guardians
            # Find if we're already at optimal position (can see guardians at range 2)
            can_see_guardian = any(rid in adjacent_ids for rid in guardian_nearby_regions)
            
            if can_see_guardian:
                # OPTIMAL POSITION: We can shoot guardians from here!
                # Check for threats: enemies entering our region (range 0)
                if enemies_here:
                    # Check if enemy is weak (fist user) - kill immediately!
                    # Use autonomous AI parameters for dynamic threshold
                    weak_threshold = autonomous_ai.strategy_params.weak_enemy_threshold
                    aggression_mod = autonomous_ai.strategy_params.aggression_level
                    
                    weak_enemies = [e for e in enemies_here if e.get("hp", 100) <= weak_threshold or 
                                   (e.get("equippedWeapon") is None or 
                                    e.get("equippedWeapon", {}).get("typeId", "") == "fist")]
                    
                    # Apply aggression modifier to decision making
                    should_attack = weak_enemies and ep >= COMBAT_MIN_EP
                    if should_attack and aggression_mod < 0.5:
                        # Low aggression - be more selective
                        weak_enemies = [e for e in weak_enemies if e.get("hp", 100) <= weak_threshold * 0.8]
                    
                    if weak_enemies and ep >= COMBAT_MIN_EP:
                        target = _select_weakest(weak_enemies)
                        log.info("🏹 SNIPER_EASY_KILL: Weak enemy %s (HP=%d Weapon=%s) in same region - KILL!",
                                target.get('name','?'), target.get('hp',0), 
                                target.get('equippedWeapon', {}).get('typeId', 'fist'))
                        return {"action": "attack",
                                "data": {"targetId": target["id"], "targetType": "agent"},
                                "reason": f"SNIPER_EASY_KILL: Weak target {target.get('name','?')} with {target.get('equippedWeapon', {}).get('typeId', 'fist')}"}
                    
                    # THREAT! Strong enemy in same region - KITE AWAY!
                    log.warning("🏹 SNIPER_KITE: Strong enemy in region! HP=%d, fleeing to maintain range", hp)
                    safe_conns = [c for c in connections if _get_region_id(c) not in danger_ids 
                                  and _get_region_id(c) not in guardian_nearby_regions]
                    if safe_conns:
                        escape_rid = _get_region_id(safe_conns[0])
                        return {"action": "move", "data": {"regionId": escape_rid},
                                "reason": "SNIPER_KITE: Strong enemy too close, maintaining range 2 advantage"}
                
                # Check enemies in range 2 (adjacent) - AGGRESSIVE SNIPER
                if enemies_in_range and ep >= COMBAT_MIN_EP:
                    # Priority 1: Weak enemies (fist users, HP <= 60)
                    weak_enemies = [e for e in enemies_in_range if e.get("hp", 100) <= 60 or 
                                   (e.get("equippedWeapon") is None or 
                                    e.get("equippedWeapon", {}).get("typeId", "") == "fist")]
                    if weak_enemies:
                        target = _select_weakest(weak_enemies)
                        log.info("🏹 SNIPER_AGGRESSIVE: Weak enemy %s (HP=%d Weapon=%s) in range 2 - KILL!",
                                target.get('name','?'), target.get('hp',0), 
                                target.get('equippedWeapon', {}).get('typeId', 'fist'))
                        return {"action": "attack",
                                "data": {"targetId": target["id"], "targetType": "agent"},
                                "reason": f"SNIPER_AGGRESSIVE: Weak target {target.get('name','?')} with {target.get('equippedWeapon', {}).get('typeId', 'fist')}"}
                    
                    # Priority 2: Any enemy (sniper advantage - they can't melee counter)
                    target = _select_best_target(
                        enemies_in_range, atk, equipped, defense, region_weather,
                        my_hp=hp, alive_count=alive_count
                    )
                    if target:
                        log.info("🏹 SNIPER_DOMINANCE: Attacking %s (HP=%d) with range advantage",
                                target["agent"].get('name','?'), target["agent"].get('hp','?'))
                        return {"action": "attack",
                                "data": {"targetId": target["agent"]["id"], "targetType": "agent"},
                                "reason": f"SNIPER_DOMINANCE: Range advantage vs {target['agent'].get('name','?')}"}
                
                # SAFE POSITION: Rest and scan for guardians
                if hp < 80 or ep < 8:
                    log.info("🏹 SNIPER_CAMP: Optimal position, resting (HP=%d EP=%d)", hp, ep)
                    return {"action": "rest", "data": {},
                            "reason": "SNIPER_CAMP: Range 2 from guardians, recovering resources"}
                
                # Attack guardian if in range 2
                for g in guardians:
                    g_region = g.get("regionId", "")
                    if g_region in adjacent_ids and ep >= COMBAT_MIN_EP:
                        log.info("🏹 SNIPER_SHOT: Guardian at range 2, safe to engage")
                        return {"action": "attack",
                                "data": {"targetId": g["id"], "targetType": "agent"},
                                "reason": "SNIPER: Guardian at optimal range 2"}
            else:
                # NOT IN POSITION: Move to get range 2 on guardians
                # Find connection that puts us adjacent to guardian
                for conn in connections:
                    conn_rid = _get_region_id(conn)
                    # Check if this region is adjacent to any guardian region
                    if conn_rid not in danger_ids:
                        # Check connections of this region
                        resolved = _resolve_region(conn, {"visibleRegions": visible_regions})
                        if resolved:
                            conn_connections = resolved.get("connections", [])
                            conn_adjacent = set(_get_region_id(c) for c in conn_connections)
                            if any(g_rid in conn_adjacent for g_rid in guardian_nearby_regions):
                                log.info("🏹 SNIPER_APPROACH: Moving to range 2 position near guardian")
                                return {"action": "move", "data": {"regionId": conn_rid},
                                        "reason": "SNIPER_APPROACH: Positioning for range 2 shot on guardian"}

    # ── Priority 8: Guardian farming (120 sMoltz per kill!) ────────
    # Only farm if: HP is safe + we can win the fight + EP budget for chase
    guardians = [a for a in visible_agents
                 if a.get("isGuardian", False) and a.get("isAlive", True)]
    guardian_weapon_ok = w_type in ("katana", "sniper", "sword", "pistol")
    guardian_heal_ok = healing_count >= 1 or hp >= 75
    nearby_players = len(enemies_here) + len(enemies_in_range)
    guardian_weather_ok = region_weather not in ("storm", "fog") or w_range >= 1
    if (guardians and hp >= max(GUARDIAN_FARM_MIN_HP, 60) and guardian_weapon_ok
            and guardian_heal_ok and nearby_players == 0 and guardian_weather_ok):
        # EP budget: combat EP + move EP for potential chase
        ep_budget = COMBAT_MIN_EP + move_ep_cost + ep_reserve
        if ep >= ep_budget:
            target = _select_best_target(
                guardians, atk, equipped, defense, region_weather,
                my_hp=hp, alive_count=alive_count
            )
            if target:
                if _is_in_range(target["agent"], region_id, w_range, connections):
                    return {"action": "attack",
                            "data": {"targetId": target["agent"]["id"], "targetType": "agent"},
                            "reason": f"GUARDIAN FARM: HP={target['agent'].get('hp','?')} "
                                      f"(120 sMoltz! dmg={target['my_dmg']} vs {target['enemy_dmg']})"}
    elif guardians:
        log.info("GUARDIAN_SKIP: hp=%d weapon_ok=%s heal_ok=%s nearby_players=%d weather_ok=%s",
                 hp, guardian_weapon_ok, guardian_heal_ok, nearby_players, guardian_weather_ok)

    # ── Priority 7: Monster farming (only when EP is abundant) ────
    monsters = [m for m in visible_monsters if m.get("hp", 0) > 0]
    if monsters and ep >= (COMBAT_MIN_EP + ep_reserve) and hp >= 30:
        target = _select_weakest(monsters)
        if _is_in_range(target, region_id, w_range, connections):
            return {"action": "attack",
                    "data": {"targetId": target["id"], "targetType": "monster"},
                    "reason": f"MONSTER FARM: {target.get('name', 'monster')} HP={target.get('hp', '?')}"}

    # ── Priority 7b: Moderate healing (safe area, no enemies) ─────
    if hp < HP_MODERATE_THRESHOLD and not enemies_here:
        heal = _find_healing_item(inventory, critical=(hp < HP_CRITICAL_THRESHOLD))
        if heal:
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"HEAL: HP={hp}, area safe, using {heal.get('typeId', 'heal')}"}

    # ── Priority 8: Facility interaction ──────────────────────────
    # Facility interact EP cost = 1 (per game-guide.md)
    if interactables and ep >= 1 and not region.get("isDeathZone"):
        facility = _select_facility(interactables, hp, ep, inventory)
        if facility:
            return {"action": "interact",
                    "data": {"interactableId": facility["id"]},
                    "reason": f"FACILITY: {facility.get('type', 'unknown')}"}

    # ── Priority 8b: FACILITY CAMPING (Stay and use facility if low) ───────
    # If HP is critically low, MUST use medical facility for healing, not just rest!
    has_medical = any(f.get("type", "").lower() == "medical_facility" and not f.get("isUsed")
                      for f in interactables if isinstance(f, dict))
    has_supply = any(f.get("type", "").lower() == "supply_cache" and not f.get("isUsed")
                     for f in interactables if isinstance(f, dict))
    
    # CRITICAL: If HP low and medical facility available, USE IT immediately!
    # Medical facility: EP cost = 1, restores some HP, NOT reusable (isUsed check)
    if has_medical and hp < HP_CRITICAL_THRESHOLD and ep >= 1:
        medical_fac = next((f for f in interactables 
                            if isinstance(f, dict) 
                            and f.get("type", "").lower() == "medical_facility"
                            and not f.get("isUsed")), None)
        if medical_fac:
            log.warning("🚨 CRITICAL HEAL at medical facility — HP=%d, using facility NOW!", hp)
            return {"action": "interact",
                    "data": {"interactableId": medical_fac["id"]},
                    "reason": f"CRITICAL_HEAL: Using medical facility (HP={hp})"}
    
    # Moderate HP: camp and rest at facility
    if (has_medical or has_supply) and (hp < 70 or ep < 8) and not items_here and not guardians_here:
        log.info("🏕️ Camping at facility — HP=%d EP=%d, resting to recover...", hp, ep)
        return {"action": "rest", "data": {},
                "reason": f"CAMPING: Resting at facility to recover (HP={hp} EP={ep})"}

    # ── Priority 9: ACTIVE HUNTING (All phases - seek out enemies!) ──
    # Bot should ALWAYS hunt kills, not just late game! Target = maximum kills
    is_late_game = alive_count <= 25
    is_endgame = alive_count <= 10
    is_ready_to_hunt = hp >= 30 and ep >= 4  # Lower threshold for aggressive hunting
    
    # Hunt at ALL game phases if ready and no enemies nearby
    # TIME EFFICIENT: Maximum kills in 59 turns = 1 kill per turn target
    is_ready_to_hunt = hp >= 25 and ep >= 3  # Even lower threshold for time efficiency
    if is_ready_to_hunt and not enemies_here and not enemies_in_range:
        # Cari region dengan musuh untuk dihunt
        best_hunt_target = None
        best_hunt_score = -1
        for conn in connections:
            rid = _get_region_id(conn)
            if not rid or rid in danger_ids:
                continue
            enemy_count = enemy_region_count.get(rid, 0)
            if enemy_count > 0:
                # TIME EFFICIENT SCORING: Prioritize quick kills
                # Early game (Turns 1-20): Maximum aggression for early kills
                # Mid game (Turns 21-40): Sustained hunting pressure  
                # Endgame (Turns 41-59): Desperation mode - any target
                
                if alive_count >= 50:  # Early game - maximum aggression
                    score = 80 if enemy_count <= 2 else 40  # Very high early game bonus
                    score += 30  # Time efficiency bonus
                elif alive_count >= 25:  # Mid game - balanced hunting
                    score = 60 if enemy_count <= 2 else 20
                    score += 20  # Moderate time bonus
                else:  # Endgame - desperation
                    score = 70 if enemy_count <= 3 else 50  # Willing to fight larger groups
                    score += 40  # Maximum desperation bonus
                
                # Distance penalty - prefer closer targets for time efficiency
                distance_penalty = 0
                if rid in _visited_regions:
                    distance_penalty = 10  # Prefer new regions for exploration
                
                score -= distance_penalty
                if score > best_hunt_score:
                    best_hunt_score = score
                    best_hunt_target = rid
        
        if best_hunt_target:
            hunt_phase = "ENDGAME" if is_endgame else ("LATE_GAME" if is_late_game else "EARLY_GAME")
            log.info("🎯 ACTIVE_HUNTING: %s (%d alive), seeking enemies at %s", 
                     hunt_phase, alive_count, best_hunt_target[:8])
            _track_chase()
            return {"action": "move", "data": {"regionId": best_hunt_target},
                    "reason": f"ACTIVE_HUNTING: Seeking kills ({hunt_phase}, {alive_count} alive)"}

    # ── Priority 9b: Strategic movement ────────────────────────────
    # COMBAT PRIORITY: Only move if no immediate combat opportunities
    if enemies_in_range and w_range >= 1 and ep >= MIN_EP_FOR_COMBAT and can_afford_combat:
        log.warning("🚨 COMBAT_PRIORITY: %d enemies in range but movement selected - FORCING COMBAT!", len(enemies_in_range))
        # Force attack instead of movement
        weakest = _select_weakest(enemies_in_range)
        if weakest:
            _track_attack(attack_type="ranged")
            return {"action": "attack",
                    "data": {"targetId": weakest["id"], "targetType": "agent"},
                    "reason": f"FORCE_COMBAT: Attacking {weakest.get('name','?')} (range advantage) instead of movement"}
    
    # Only move if no immediate combat opportunities
    if (not enemies_here and not enemies_in_range) or ep < MIN_EP_FOR_COMBAT:
        # In empty free rooms, avoid aimless wandering that wastes EP
        has_targets = (len(visible_items) > 0 or
                       any(f for f in interactables if isinstance(f, dict) and not f.get("isUsed")) or
                       len(visible_agents) > 0)

        # WEATHER DELAY: avoid unnecessary movement in storm
        weather_delay = (region_weather == "storm" and not has_targets and ep < 6)
        if weather_delay:
            log.info("WEATHER_DELAY: Storm + no targets + low EP. Waiting for clear weather.")
            return None  # Skip movement, rest instead
    weather_delay = (region_weather == "storm" and not has_targets and ep < 6)
    if weather_delay:
        log.info("WEATHER_DELAY: Storm + no targets + low EP. Waiting for clear weather.")
        return None  # Skip movement, rest instead

    if ep >= move_ep_cost and connections:
        move_target = _choose_move_target(connections, danger_ids,
                                           region, visible_items, alive_count,
                                           visible_agents, self_data.get("id", ""), hp, ep,
                                           visible_regions, equipped, inventory)
        if move_target and move_target != region_id:
            return {"action": "move", "data": {"regionId": move_target},
                    "reason": "EXPLORE: Moving to seek targets or vision"}
        
        # If best move is current region, but we have no targets, try any unvisited connection
        if not has_targets and ep >= 4:
            for conn in connections:
                rid = _get_region_id(conn)
                resolved = _resolve_region(conn, {"visibleRegions": visible_regions})
                terrain = resolved.get("terrain", "").lower() if resolved else ""
                enemy_count = enemy_region_count.get(rid, 0)
                
                score = 10  # Base score for movement
                if rid not in danger_ids and rid not in _visited_regions:
                    return {"action": "move", "data": {"regionId": rid},
                            "reason": "EXPLORE: Forcing move to unvisited region"}

    log.info("IDLE: Staying in place (EP=%d, Targets=%s)", ep, has_targets)

    # ── Priority 9b: BOREDOM MOVEMENT ───────────────────────────────
    # If staying idle too long with full EP, force movement to random safe region
    # This prevents getting stuck when all nearby regions visited
    _consecutive_idle_turns = getattr(decide_action, '_consecutive_idle_turns', 0)
    
    if not has_targets and ep >= 8 and not enemies_here:
        _consecutive_idle_turns += 1
        if _consecutive_idle_turns >= 3:  # 3 turns idle with full EP
            # Force move to any random safe connected region
            for conn in connections:
                rid = _get_region_id(conn)
                if rid and rid not in danger_ids and rid != region_id:
                    log.info("BOREDOM_MOVE: Forcing movement after %d idle turns", _consecutive_idle_turns)
                    _consecutive_idle_turns = 0
                    decide_action._consecutive_idle_turns = 0
                    return {"action": "move", "data": {"regionId": rid},
                            "reason": "BOREDOM: Forcing move after idle with full EP"}
    else:
        _consecutive_idle_turns = 0
    decide_action._consecutive_idle_turns = _consecutive_idle_turns

    # ── Priority 10: Rest (EP < 4 and safe) ───────────────────────
    # Also rest if weather is storm and no urgent targets
    if (ep < 4 or weather_delay) and not enemies_here and not region.get("isDeathZone") and region_id not in danger_ids:
        return {"action": "rest", "data": {},
                "reason": f"REST: EP={ep}/{max_ep}, area is safe (+1 bonus EP)"}

    return None  # Wait for next turn


# ── Helper functions ──────────────────────────────────────────────────

def _get_move_ep_cost(terrain: str, weather: str) -> int:
    """Calculate move EP cost per game-systems.md.
    Base: 2. Storm: +1. Water terrain: 3.
    """
    if terrain == "water":
        return 3
    if weather == "storm":
        return 3  # 2 base + 1 storm
    return 2


def _estimate_enemy_weapon_bonus(agent: dict) -> int:
    """Estimate enemy's weapon bonus from their equipped weapon."""
    weapon = agent.get("equippedWeapon")
    if not weapon:
        return 0
    type_id = weapon.get("typeId", "").lower() if isinstance(weapon, dict) else ""
    return WEAPONS.get(type_id, {}).get("bonus", 0)


def _estimate_enemy_strength(agent: dict) -> dict:
    """
    Comprehensive enemy strength estimation.
    
    Returns dict with:
    - weapon_bonus: ATK bonus from weapon
    - weapon_range: Range of weapon (0=melee, 1+=ranged)
    - weapon_type: Weapon typeId
    - ep: Enemy EP (action points available)
    - estimated_heals: Estimated healing items (based on inventory hints)
    - effective_hp: HP + estimated heal potential
    - threat_level: 0-100 (higher = more dangerous)
    """
    # Basic stats
    hp = agent.get("hp", 100)
    ep = agent.get("ep", 10)
    atk = agent.get("atk", 10)
    defense = agent.get("def", 5)
    
    # Weapon analysis
    weapon = agent.get("equippedWeapon")
    weapon_type = "fist"
    weapon_bonus = 0
    weapon_range = 0
    
    if weapon:
        if isinstance(weapon, dict):
            weapon_type = weapon.get("typeId", "").lower()
        elif isinstance(weapon, str):
            weapon_type = weapon.lower()
        weapon_bonus = WEAPONS.get(weapon_type, {}).get("bonus", 0)
        weapon_range = WEAPONS.get(weapon_type, {}).get("range", 0)
    
    # Estimate healing items from inventory (if visible) or default assumptions
    inventory = agent.get("inventory", [])
    estimated_heals = 0
    heal_potential = 0
    
    if inventory and isinstance(inventory, list):
        for item in inventory:
            if isinstance(item, dict):
                item_type = item.get("typeId", "").lower()
                if item_type in RECOVERY_ITEMS:
                    estimated_heals += 1
                    heal_potential += RECOVERY_ITEMS[item_type]
    else:
        # No inventory visibility - assume average player has 1-2 heals mid/late game
        estimated_heals = 1 if hp > 50 else 0
        heal_potential = estimated_heals * 25  # Average 25 HP per heal
    
    # Effective HP = current HP + potential heal
    effective_hp = hp + heal_potential
    
    # Calculate threat level (0-100)
    threat_level = 0
    
    # Base threat from HP
    threat_level += min(30, hp / 3.3)  # Full HP = 30 pts
    
    # Weapon threat (katana/sniper = high threat)
    if weapon_type in ("katana", "sniper"):
        threat_level += 25
    elif weapon_type in ("sword", "pistol"):
        threat_level += 15
    elif weapon_type in ("dagger", "bow"):
        threat_level += 8
    
    # EP threat (reduced weight - EP=1 should not be major threat)
    threat_level += min(10, ep * 1)  # 10 EP = 10 pts (reduced from 20)
    
    # EP disadvantage penalty (low EP = less dangerous)
    if ep <= 2:
        threat_level -= 15  # Significant penalty for very low EP
    
    # Heal potential threat (sustainability)
    threat_level += min(15, estimated_heals * 5)  # 3 heals = 15 pts
    
    # Ranged weapons extra threat (can't kite them easily)
    if weapon_range >= 1:
        threat_level += 10
    
    return {
        "weapon_bonus": weapon_bonus,
        "weapon_range": weapon_range,
        "weapon_type": weapon_type,
        "ep": ep,
        "estimated_heals": estimated_heals,
        "heal_potential": heal_potential,
        "effective_hp": effective_hp,
        "threat_level": min(100, threat_level),
        "atk": atk,
        "def": defense,
        "hp": hp,
    }


def _get_adjacent_ids(region_obj, visible_regions: list = None) -> list:
    """Extract all adjacent region IDs from a region object or ID string."""
    if isinstance(region_obj, str):
        # If we only have an ID, we need to find the full object in visibleRegions
        if visible_regions:
            for r in visible_regions:
                if isinstance(r, dict) and r.get("id") == region_obj:
                    return _get_adjacent_ids(r)
        return []
        
    adj = []
    conns = region_obj.get("connections", [])
    for c in conns:
        if isinstance(c, str):
            adj.append(c)
        elif isinstance(c, dict):
            adj.append(c.get("id", ""))
    return adj


def _select_best_target(targets: list, my_atk: int, my_equipped,
                        my_def: int, weather: str,
                        my_hp: int = 100, alive_count: int = 100) -> dict | None:
    """Smart target selection — pick the most favorable fight.
    Returns dict with {agent, my_dmg, enemy_dmg, should_kite} or None.
    Priorities:
    1. Wounded enemies we can ONE-SHOT (free kill!)
    2. Enemies where we have clear damage advantage
    3. Late game: take riskier fights when alive_count < 10
    """
    my_bonus = get_weapon_bonus(my_equipped)
    best = None
    best_score = -999
    is_late_game = alive_count <= 10
    is_endgame = alive_count <= 5

    for t in targets:
        t_def = t.get("def", 5)
        t_atk = t.get("atk", 10)
        t_hp  = t.get("hp", 100)
        t_weapon_bonus = _estimate_enemy_weapon_bonus(t)

        my_dmg    = calc_damage(my_atk, my_bonus, t_def, weather)
        enemy_dmg = calc_damage(t_atk, t_weapon_bonus, my_def, weather)

        if my_dmg <= 0:
            continue

        turns_to_kill  = max(1, t_hp // max(my_dmg, 1))
        turns_to_die   = max(1, my_hp // max(enemy_dmg, 1)) if enemy_dmg > 0 else 999
        one_shot       = t_hp <= my_dmg
        two_shot       = t_hp <= my_dmg * 2
        we_outlast     = turns_to_die > turns_to_kill  # We kill before they kill us
        we_trade_up    = my_dmg >= enemy_dmg

        # ── Scoring ──────────────────────────────────────────────
        score = 0

        # One-shot / two-shot = huge bonus (free or cheap kill)
        if one_shot:
            score += 200
        elif two_shot:
            score += 80

        # Damage advantage
        score += (my_dmg - enemy_dmg) * 3

        # Survival advantage
        if we_outlast:
            score += 40
        else:
            score -= (turns_to_kill - turns_to_die) * 20  # Penalty for dying first

        # Late game: be more willing to fight
        if is_endgame:
            score += 50
        elif is_late_game:
            score += 25

        # Penalize tanky targets that survive long
        score -= turns_to_kill * 5

        # Only accept fight if:
        # - We can one/two-shot them, OR
        # - We have damage advantage AND survive the fight, OR
        # - Late game (must fight to win)
        acceptable = (one_shot or two_shot
                      or (we_trade_up and we_outlast)
                      or (is_late_game and turns_to_die >= turns_to_kill))

        if acceptable and score > best_score:
            best_score = score
            # Kite if: we have ranged weapon AND enemy is stronger
            should_kite = (get_weapon_range(my_equipped) >= 1
                           and enemy_dmg > my_dmg
                           and not one_shot)
            best = {"agent": t, "my_dmg": my_dmg,
                    "enemy_dmg": enemy_dmg, "should_kite": should_kite}

    return best


def _get_combat_hp_threshold(alive_count: int, equipped) -> int:
    """Adaptive HP threshold for entering combat.
    Depends on: game phase (alive count), weapon quality, aggression config.
    """
    weapon_bonus = get_weapon_bonus(equipped) if equipped else 0

    # Base thresholds per aggression level
    if AGGRESSION_LEVEL == "aggressive":
        base = 20
    elif AGGRESSION_LEVEL == "passive":
        base = 50
    else:  # balanced
        base = 35

    # Late game: lower threshold (more aggressive when fewer players)
    if alive_count <= 5:
        base -= 10
    elif alive_count <= 15:
        base -= 5

    # Good weapon = can afford to fight at lower HP
    if weapon_bonus >= 20:  # sword or better
        base -= 5

    return max(15, base)  # Never fight below 15 HP


# Track observed agents for memory (threat assessment)
_known_agents: dict = {}


# ── CURSE HANDLING — DISABLED in v1.5.2 ───────────────────────────────
# Curse is temporarily disabled per strategy.md v1.5.2.
# Guardians no longer set victim EP to 0 and no whisper-question/answer flow.
# Legacy code kept below for reference — will re-enable when curse returns.
#
# def _check_curse(messages, my_id) -> dict | None:
#     """DISABLED: Guardian curse is temporarily disabled in v1.5.2."""
#     return None
#
# def _solve_curse_question(question) -> str:
#     """DISABLED: Guardian curse is temporarily disabled in v1.5.2."""
#     return ""


def _check_pickup(items: list, inventory: list, region_id: str) -> dict | None:
    """Smart pickup: weapons > healing stockpile > utility > Moltz (always).
    Max inventory = 10 per limits.md.
    Strategy:
    - Moltz ($rewards): ALWAYS pickup, highest priority
    - Weapons: pickup if better than current OR no weapon equipped
    - Healing: stockpile for endgame (keep at least 2-3 healing items)
    - Binoculars: passive vision+1, always pickup
    - Map: pickup and use immediately
    """
    # Filter items in current region (items may lack regionId field)
    local_items = [i for i in items
                   if isinstance(i, dict) and i.get("regionId") == region_id]
    # Fallback: if regionId filter found nothing, use all visible items
    # (the game may not set regionId on item objects)
    if not local_items:
        local_items = [i for i in items if isinstance(i, dict) and i.get("id")]
    if not local_items:
        return None

    # Count current healing items for stockpile management
    heal_count = sum(1 for i in inventory if isinstance(i, dict)
                     and i.get("typeId", "").lower() in RECOVERY_ITEMS
                     and RECOVERY_ITEMS.get(i.get("typeId", "").lower(), 0) > 0)

    # SMART INVENTORY: If full, check if we should use inferior items first
    if len(inventory) >= 10:
        # Try to use low-value items to make space for better ones
        usage_action = _try_use_low_value_items(inventory, heal_count)
        if usage_action:
            return usage_action
        
        # If no usage possible, check if new item is significantly better
        best_replacement = _find_best_replacement(local_items, inventory, heal_count)
        if best_replacement:
            item_to_drop, item_to_pickup = best_replacement
            log.info("SMART_INVENTORY: Found better item %s > %s, but no drop action available", 
                    item_to_pickup.get("typeId", "item"), item_to_drop.get("typeId", "item"))
            # Since game doesn't have drop action, we'll skip pickup and wait for natural turnover
            log.info("PICKUP: Inventory full (%d/10) — waiting for natural turnover", len(inventory))
            return None
        else:
            log.info("PICKUP: Inventory full (%d/10) — no valuable replacements", len(inventory))
            return None

    # Sort by priority — Moltz always first
    local_items.sort(
        key=lambda i: _pickup_score(i, inventory, heal_count), reverse=True)
    best = local_items[0]
    score = _pickup_score(best, inventory, heal_count)
    if score > 0:
        type_id = best.get('typeId', 'item')
        log.info("PICKUP: %s (score=%d, heal_stock=%d)", type_id, score, heal_count)
        return {"action": "pickup", "data": {"itemId": best["id"]},
                "reason": f"PICKUP: {type_id}"}
    return None


def _try_use_low_value_items(inventory: list, heal_count: int) -> dict | None:
    """Try to use low-value items to make space for better pickups."""
    if not inventory:
        return None
    
    # Find lowest value consumable items
    low_value_items = []
    for item in inventory:
        if not isinstance(item, dict):
            continue
        
        type_id = item.get("typeId", "").lower()
        item_id = item.get("id")
        
        # Check for recently failed usage
        action_key = f"use_item:{item_id}"
        if _failed_actions.get(action_key, 0) > _current_turn:
            continue
        
        # Priority items to use when inventory is full
        if type_id == "energy_drink":
            low_value_items.append((item, 10))  # Low priority
        elif type_id in ("emergency_food", "bandage") and heal_count > 3:
            low_value_items.append((item, 20))  # Use excess healing
        elif type_id == "medkit" and heal_count > 2:
            low_value_items.append((item, 25))  # Use excess medkits
        elif type_id == "map":
            low_value_items.append((item, 30))  # Use map to reveal
    
    if low_value_items:
        # Use lowest priority item first
        low_value_items.sort(key=lambda x: x[1])
        best_item, _ = low_value_items[0]
        type_id = best_item.get("typeId", "").lower()
        
        log.info("SMART_INVENTORY: Using low-value item %s to make space", type_id)
        return {"action": "use_item", "data": {"itemId": best_item["id"]},
                "reason": f"SMART_USE: Making space for better items"}
    
    return None


def _find_best_replacement(local_items: list, inventory: list, heal_count: int) -> tuple | None:
    """Find best item to replace when inventory is full.
    Returns (item_to_drop, item_to_pickup) or None if no valuable replacement.
    """
    if not local_items or not inventory:
        return None
    
    # Score all ground items
    ground_scores = []
    for item in local_items:
        if isinstance(item, dict):
            score = _pickup_score(item, inventory, heal_count)
            if score > 0:
                ground_scores.append((item, score))
    
    if not ground_scores:
        return None
    
    # Find best ground item
    best_ground = max(ground_scores, key=lambda x: x[1])
    best_item, best_score = best_ground
    
    # Find worst inventory item
    inventory_scores = []
    for item in inventory:
        if isinstance(item, dict):
            # Calculate current value of inventory item
            inv_score = _calculate_item_value(item, inventory, heal_count)
            inventory_scores.append((item, inv_score))
    
    if not inventory_scores:
        return None
    
    worst_inventory = min(inventory_scores, key=lambda x: x[1])
    worst_item, worst_score = worst_inventory
    
    # Only replace if ground item is significantly better (25% improvement)
    if best_score > worst_score * 1.25:
        return (worst_item, best_item)
    
    return None


def _calculate_item_value(item: dict, inventory: list, heal_count: int) -> int:
    """Calculate current value of an inventory item."""
    type_id = item.get("typeId", "").lower()
    category = item.get("category", "").lower()
    
    # Use same scoring as pickup but for existing items
    if type_id == "rewards" or category == "currency":
        return 300
    
    if category == "weapon":
        weapon_bonus = WEAPONS.get(type_id, {}).get("bonus", 0)
        return 100 + weapon_bonus
    
    if type_id in RECOVERY_ITEMS and RECOVERY_ITEMS.get(type_id, 0) > 0:
        if heal_count <= 2:
            return RECOVERY_ITEMS.get(type_id, 0) * 3  # High value when low on heals
        elif heal_count <= 4:
            return RECOVERY_ITEMS.get(type_id, 0) * 2  # Moderate value
        else:
            return RECOVERY_ITEMS.get(type_id, 0) // 2  # Low value when stocked
    
    if type_id == "binoculars":
        return 55
    
    if type_id == "map":
        return 10  # Low value if not used yet
    
    return 5  # Default low value for unknown items


def _pickup_score(item: dict, inventory: list, heal_count: int) -> int:
    """Calculate dynamic pickup score based on current inventory state."""
    type_id = item.get("typeId", "").lower()
    category = item.get("category", "").lower()

    # Moltz/sMoltz — ALWAYS pickup
    if type_id == "rewards" or category == "currency":
        return 300

    # Weapons: higher score if no weapon or this is better
    if category == "weapon":
        bonus = WEAPONS.get(type_id, {}).get("bonus", 0)
        weapon_range = WEAPONS.get(type_id, {}).get("range", 0)
        
        # Check all weapons in inventory - skip pickup if we have better or equal
        for inv_item in inventory:
            if isinstance(inv_item, dict) and inv_item.get("category") == "weapon":
                inv_type = inv_item.get("typeId", "").lower()
                inv_bonus = WEAPONS.get(inv_type, {}).get("bonus", 0)
                inv_range = WEAPONS.get(inv_type, {}).get("range", 0)
                
                # Skip if we have same weapon type already
                if inv_type == type_id:
                    return 0  # Duplicate, don't pickup
                
                # For ranged weapons: skip if we have ranged with equal or better bonus
                if weapon_range >= 1 and inv_range >= 1 and inv_bonus >= bonus:
                    return 0  # Have better or equal ranged weapon
                
                # For melee weapons: skip if we have melee with equal or better bonus
                if weapon_range == 0 and inv_range == 0 and inv_bonus >= bonus:
                    return 0  # Have better or equal melee weapon
        
        return 100 + bonus  # Better weapon = very high priority

    # Binoculars: passive vision+1 permanent, always pickup
    if type_id == "binoculars":
        has_binos = any(isinstance(i, dict) and i.get("typeId", "").lower() == "binoculars"
                       for i in inventory)
        return 55 if not has_binos else 0  # Don't stack

    # Map: only pickup if don't have any Map yet (one is enough)
    if type_id == "map":
        map_count = sum(1 for i in inventory if isinstance(i, dict) and i.get("typeId", "").lower() == "map")
        if map_count > 0:
            log.info("[MAP_TRACKING] Skip pickup | Already have %d Map(s) in inventory", map_count)
            return 0  # Don't pickup more Maps
        log.info("[MAP_TRACKING] Pickup allowed | No maps in inventory yet")
        return 52

    # Healing items: stockpile for endgame (want 3-4 items)
    if type_id in RECOVERY_ITEMS and RECOVERY_ITEMS.get(type_id, 0) > 0:
        if heal_count < 4:  # Need more healing for endgame
            return ITEM_PRIORITY.get(type_id, 0) + 10
        return ITEM_PRIORITY.get(type_id, 0)  # Normal priority

    # Energy drink
    if type_id == "energy_drink":
        return 58

    return ITEM_PRIORITY.get(type_id, 0)


def _check_equip(inventory: list, equipped) -> dict | None:
    """Auto-equip best weapon from inventory.
    
    IMPROVED: Now considers both ATK bonus AND weapon range.
    Range is valuable for survivability (can attack from distance).
    """
    current_bonus = get_weapon_bonus(equipped) if equipped else 0
    current_range = get_weapon_range(equipped) if equipped else 0
    current_weapon = equipped.get("typeId", "fist") if equipped else "fist"
    
    # Calculate current weapon value score
    # Range bonus: +10 per range level (range 2 = +20 value)
    current_value = current_bonus + (current_range * 10)
    
    best = None
    best_value = current_value
    best_bonus = current_bonus

    for item in inventory:
        if not isinstance(item, dict):
            continue
        category = item.get("category", "").lower()
        type_id = item.get("typeId", "").lower()

        if category == "weapon" or type_id in WEAPONS:
            bonus = WEAPONS.get(type_id, {}).get("bonus", 0)
            weapon_range = WEAPONS.get(type_id, {}).get("range", 0)
            
            # IMPROVED: Value = ATK bonus + (range * 10)
            # This makes Sniper (28 + 20 = 48) > Katana (35 + 0 = 35)
            value = bonus + (weapon_range * 10)
            
            log.debug("EQUIP_CHECK: %s (bonus=%d, range=%d, value=%d) vs current %s (bonus=%d, range=%d, value=%d)",
                      type_id, bonus, weapon_range, value, current_weapon, current_bonus, current_range, current_value)
            
            if value > best_value:
                best = item
                best_value = value
                best_bonus = bonus

    if best:
        best_weapon = best.get("typeId", "weapon")
        best_range = WEAPONS.get(best_weapon, {}).get("range", 0)
        log.info("EQUIP: Switching from %s (+%d, range=%d) to %s (+%d, range=%d) - value: %d > %d",
                 current_weapon, current_bonus, current_range,
                 best_weapon, best_bonus, best_range,
                 best_value, current_value)
        return {"action": "equip", "data": {"itemId": best["id"]},
                "reason": f"EQUIP: {best_weapon} (+{best_bonus} ATK, range={best_range}) vs {current_weapon} (+{current_bonus} ATK, range={current_range})"}
    return None


def _find_safe_region(connections, danger_ids: set, view: dict = None) -> str | None:
    """Find nearest connected region that's NOT a death zone AND NOT pending DZ.
    Per v1.5.2 gotchas.md §3: connectedRegions entries are EITHER full Region objects
    (when visible) OR bare string IDs (when out-of-vision). Use _resolve_region().
    danger_ids = set of all DZ + pending DZ region IDs.
    """
    safe_regions = []
    for conn in connections:
        if isinstance(conn, str):
            if conn not in danger_ids:
                safe_regions.append((conn, 0))
        elif isinstance(conn, dict):
            rid = conn.get("id", "")
            is_dz = conn.get("isDeathZone", False)
            if rid and not is_dz and rid not in danger_ids:
                terrain = conn.get("terrain", "").lower()
                score = {"hills": 3, "plains": 2, "ruins": 1, "forest": 0, "water": -2}.get(terrain, 0)
                safe_regions.append((rid, score))

    if safe_regions:
        safe_regions.sort(key=lambda x: x[1], reverse=True)
        chosen = safe_regions[0][0]
        log.debug("Safe region selected: %s (score=%d, %d candidates)",
                  chosen[:8], safe_regions[0][1], len(safe_regions))
        return chosen

    # Last resort: any non-DZ connection (even if pending)
    for conn in connections:
        rid = conn if isinstance(conn, str) else conn.get("id", "")
        is_dz = conn.get("isDeathZone", False) if isinstance(conn, dict) else False
        if rid and not is_dz:
            log.warning("No fully safe region! Using fallback: %s", rid[:8])
            return rid
    return None


def _find_healing_item(inventory: list, critical: bool = False) -> dict | None:
    """Find best healing item based on urgency.
    critical=True (HP<30): prefer Bandage(30) then Medkit(50) — big heals first
    critical=False (HP<70): prefer Emergency Food(20) — save big heals for later
    """
    heals = []
    for i in inventory:
        if not isinstance(i, dict):
            continue
        type_id = i.get("typeId", "").lower()
        if type_id in RECOVERY_ITEMS and RECOVERY_ITEMS.get(type_id, 0) > 0:
            heals.append(i)
    if not heals:
        return None

    if critical:
        # Critical: use biggest heal first (Medkit > Bandage > Emergency Food)
        heals.sort(key=lambda i: RECOVERY_ITEMS.get(i.get("typeId", "").lower(), 0), reverse=True)
    else:
        # Normal: use smallest heal first (Emergency Food first, save big heals)
        heals.sort(key=lambda i: RECOVERY_ITEMS.get(i.get("typeId", "").lower(), 0))
    return heals[0]


def _moderate_heal_in_safe_area(hp: int, inventory: list, healing_count: int, enemies_here: list, region_id: str) -> dict | None:
    """Moderate healing when in safe area (no enemies present).
    Returns healing action if HP is below moderate threshold and healing items available.
    """
    if hp >= HP_MODERATE_THRESHOLD:
        return None
    if not healing_count:
        return None
    # Find a healing item (non-critical, use small heals first)
    heal = _find_healing_item(inventory, critical=False)
    if heal:
        return {"action": "use_item", "data": {"itemId": heal["id"]},
                "reason": f"MODERATE HEAL: HP={hp}<{HP_MODERATE_THRESHOLD}, safe area, using {heal.get('typeId', 'heal')}"}
    return None


def _find_energy_drink(inventory: list) -> dict | None:
    """Find energy drink for EP recovery (+5 EP per combat-items.md)."""
    for i in inventory:
        if isinstance(i, dict) and i.get("typeId", "").lower() == "energy_drink":
            return i
    return None


def _select_weakest(targets: list) -> dict:
    """Select target with lowest HP."""
    return min(targets, key=lambda t: t.get("hp", 999))


def _is_in_range(target: dict, my_region: str, weapon_range: int,
                  connections=None) -> bool:
    """Check if target is in weapon range.
    Per combat-items.md: melee = same region, ranged = 1-2 regions.
    """
    target_region = target.get("regionId", "")

    # No regionId on target — assume same region (visible agents in same region)
    if not target_region:
        return True

    if target_region == my_region:
        return True  # Same region — melee and ranged both work

    if weapon_range >= 1 and connections:
        # Check if target is in an adjacent region (range 1+)
        adj_ids = set()
        for conn in connections:
            if isinstance(conn, str):
                adj_ids.add(conn)
            elif isinstance(conn, dict):
                adj_ids.add(conn.get("id", ""))
        if target_region in adj_ids:
            return True

    # Target is out of weapon range
    return False


def _select_facility(interactables: list, hp: int, ep: int, inventory: list = None) -> dict | None:
    """Select best facility to interact with per game-systems.md.
    Priority: medical (if HP < 80) > supply_cache > watchtower > broadcast_station.
    Cave = stealth (-2 vision) — AVOID (trap potential, limits awareness).
    Watchtower = vision boost — HIGH VALUE for scouting.
    Dynamic scoring: supply cache bonus when inventory low.
    """
    if inventory is None:
        inventory = []
    
    # Score-based selection
    best = None
    best_score = -1
    for fac in interactables:
        if not isinstance(fac, dict):
            continue
        if fac.get("isUsed"):
            continue
        ftype = fac.get("type", "").lower()
        score = 0
        if ftype == "medical_facility" and hp < 80:
            score = 10 + (80 - hp)  # More valuable when HP is lower
        elif ftype == "supply_cache":
            score = 8  # Good loot
            # Dynamic bonus: more valuable when inventory is empty
            if len(inventory) < 3:
                score += 5
            elif len(inventory) < 5:
                score += 3
        elif ftype == "watchtower":
            score = 7  # Vision boost = strategic advantage
        elif ftype == "broadcast_station":
            score = 0  # WASTED TURN: Does nothing for survival/combat
        elif ftype == "cave":
            score = 0  # AVOID: -2 vision = trap, limits awareness
        if score > best_score:
            best_score = score
            best = fac
    return best if best_score > 0 else None


def _track_agents(visible_agents: list, my_id: str, my_region: str):
    """Track observed agents for threat assessment (agent-memory.md temp.knownAgents)."""
    global _known_agents
    for agent in visible_agents:
        if not isinstance(agent, dict):
            continue
        aid = agent.get("id", "")
        if not aid or aid == my_id:
            continue
        # Extract weapon name for display
        weapon = agent.get("equippedWeapon")
        weapon_name = "?"
        if isinstance(weapon, dict):
            weapon_name = weapon.get("typeId", "?")
        elif isinstance(weapon, str):
            weapon_name = weapon

        _known_agents[aid] = {
            "hp": agent.get("hp", 100),
            "atk": agent.get("atk", 10),
            "ep": agent.get("ep", "?"),
            "isGuardian": agent.get("isGuardian", False),
            "equippedWeapon": agent.get("equippedWeapon"),
            "weaponName": weapon_name,
            "lastSeen": my_region,
            "isAlive": agent.get("isAlive", True),
        }
    # Limit size
    if len(_known_agents) > 50:
        # Remove dead agents first
        dead = [k for k, v in _known_agents.items() if not v.get("isAlive", True)]
        for d in dead:
            del _known_agents[d]


def _detect_guardians_from_whispers(messages: list, my_region: str, connections: list, visible_regions: list):
    """Detect guardian locations from whisper messages.
    Guardians whisper to players in same region (30% chance per turn per docs).
    If we receive a whisper, the guardian is likely in our current region or adjacent.
    """
    global _guardian_locations
    if not messages:
        return
    
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        # Check if it's a whisper
        msg_type = msg.get("type", "").lower()
        if msg_type not in ("whisper", "private"):
            continue
        
        sender = msg.get("sender", {}).get("name", "") if isinstance(msg.get("sender"), dict) else ""
        content = msg.get("content", "") or msg.get("message", "")
        sender_id = msg.get("senderId", "")
        
        # Check if sender might be a guardian (name patterns or ID patterns)
        is_likely_guardian = (
            "guardian" in sender.lower() or
            (isinstance(sender_id, str) and "guardian" in sender_id.lower())
        )
        
        if is_likely_guardian:
            # Guardian whispers from their location
            # If we can see them in visible_regions, they might be nearby
            log.info("[GUARDIAN_DETECT] Whisper from %s: '%s...' | Likely in/near %s",
                     sender, content[:30], my_region[:8])
            
            # Mark current region and adjacent as possible guardian locations
            _guardian_locations[my_region] = True
            
            # Also check if sender regionId is provided in message
            sender_region = msg.get("sender", {}).get("regionId", "") if isinstance(msg.get("sender"), dict) else ""
            if sender_region:
                _guardian_locations[sender_region] = True
                log.info("[GUARDIAN_DETECT] Guardian location confirmed: %s", sender_region[:8])


def _track_guardians(visible_agents: list, my_region: str):
    """Track guardian locations for active hunting (120 sMoltz per kill)."""
    global _guardian_locations
    for agent in visible_agents:
        if not isinstance(agent, dict):
            continue
        if agent.get("isGuardian", False) and agent.get("isAlive", True):
            rid = agent.get("regionId", my_region)
            _guardian_locations[rid] = True  # Mark region as having guardian


def _find_safe_region_with_exit(connections, danger_ids: set, view: dict = None) -> str | None:
    """Find safe region that also has exit options (avoid dead ends).
    Used for retreat path planning.
    """
    safe_regions = []
    for conn in connections:
        if isinstance(conn, str):
            if conn not in danger_ids:
                safe_regions.append((conn, 0))
        elif isinstance(conn, dict):
            rid = conn.get("id", "")
            is_dz = conn.get("isDeathZone", False)
            if rid and not is_dz and rid not in danger_ids:
                terrain = conn.get("terrain", "").lower()
                score = {"hills": 3, "plains": 2, "ruins": 1, "forest": 0, "water": -2}.get(terrain, 0)
                # Bonus for regions with more connections (better exit options)
                conns = conn.get("connections", [])
                score += len(conns) * 0.5
                safe_regions.append((rid, score))

    if safe_regions:
        safe_regions.sort(key=lambda x: x[1], reverse=True)
        return safe_regions[0][0]
    return None


def _use_utility_item(inventory: list, hp: int, ep: int, alive_count: int) -> dict | None:
    """Use utility items immediately after pickup.
    Map: CONSUMABLE (1-time) — use immediately to reveal entire map.
    Binoculars: PASSIVE (vision+1 just by holding) — no use_item needed.
    """
    for item in inventory:
        if not isinstance(item, dict):
            continue
        type_id = item.get("typeId", "").lower()
        item_id = item.get("id")

        # Skip if this specific item action failed recently
        action_key = f"use_item:{item_id}"
        if _failed_actions.get(action_key, 0) > _current_turn:
            continue

        # NOTE: Map usage via use_item FAILS with "Cannot use this item"
        # Map appears to be either:
        # 1. Auto-reveal on pickup (passive activation)
        # 2. Already consumed but still showing in inventory (ghost item)
        # 3. Different mechanic than documented
        # We no longer try to use Map - just prevent picking up multiple.

        # Energy Drink: use if EP is low
        if type_id == "energy_drink" and ep <= 5:
            return {"action": "use_item", "data": {"itemId": item_id},
                    "reason": "UTILITY: Using Energy Drink (+5 EP)"}
                    
        # Megaphone: use if we want to broadcast (low priority)
        # if type_id == "megaphone": ...

    return None


def learn_from_map(view: dict):
    """Called after Map is used — learn entire map layout.
    Track all death zones, pending DZ, and find safe center regions.
    Per game-guide.md: Map reveals entire map (1-time consumable).
    """
    global _map_knowledge
    visible_regions = view.get("visibleRegions", [])
    if not visible_regions:
        return

    _map_knowledge["revealed"] = True
    safe_regions = []

    for region in visible_regions:
        if not isinstance(region, dict):
            continue
        rid = region.get("id", "")
        if not rid:
            continue

        if region.get("isDeathZone"):
            _map_knowledge["death_zones"].add(rid)
        else:
            # Count connections — center regions have more connections
            conns = region.get("connections", [])
            terrain = region.get("terrain", "").lower()
            terrain_value = {"hills": 3, "plains": 2, "ruins": 2, "forest": 1, "water": -1}.get(terrain, 0)
            score = len(conns) + terrain_value
            safe_regions.append((rid, score))

    # Sort by connectivity+terrain — highest = most likely center
    safe_regions.sort(key=lambda x: x[1], reverse=True)
    _map_knowledge["safe_center"] = [r[0] for r in safe_regions[:5]]

    log.info("🗺️ MAP LEARNED: %d DZ regions, %d safe regions, top center: %s",
             len(_map_knowledge["death_zones"]),
             len(safe_regions),
             _map_knowledge["safe_center"][:3])


def _choose_move_target(connections, danger_ids: set,
                         current_region: dict, visible_items: list,
                         alive_count: int, visible_agents: list = None,
                         my_id: str = "", my_hp: int = 100,
                         ep: int = 10,
                         visible_regions: list = None,
                         equipped = None,
                         inventory: list = None) -> str | None:
    """Choose best region to move to.
    CRITICAL: NEVER move into a death zone or pending death zone!
    Enhanced: avoid regions with many enemies when HP is low.
    New: visited region penalty, guardian hunting, weather delay, late game hunt.
    """
    global _visited_regions, _guardian_locations, _map_knowledge
    candidates = []
    # Pre-calculate hunter readiness
    w_type = equipped.get("typeId", "").lower() if isinstance(equipped, dict) else ""
    has_weapon = w_type in ("katana", "sniper", "sword", "pistol", "dagger", "bow")
    healing_count = len([i for i in inventory if i.get("typeId", "").lower() in RECOVERY_ITEMS]) if inventory else 0
    is_ready_for_war = has_weapon and healing_count >= 1 and my_hp >= 60
    is_late_game = alive_count <= 25
    is_endgame = alive_count <= 10
    
    # IMPROVED: Safe exit route priority - reduce exploration bonus when low HP
    # Safety matters more than new regions when vulnerable
    hp_ratio = my_hp / 100
    visited_penalty = 20 if is_late_game else max(20, int(60 * hp_ratio))  # Less penalty when HP low
    new_region_bonus = 20 if is_late_game else max(10, int(45 * hp_ratio))  # Less bonus when HP low

    # Build region item attractiveness scores
    item_region_scores = {}
    items_with_rid = 0
    for item in visible_items:
        if not isinstance(item, dict):
            continue
        rid = item.get("regionId", "")
        if not rid:
            continue
        items_with_rid += 1
        type_id = item.get("typeId", "").lower()
        category = item.get("category", "").lower()
        score = 0

        # Weapons: high score if valuable and better than current
        if category == "weapon":
            w_bonus = WEAPONS.get(type_id, {}).get("bonus", 0)
            score += w_bonus // 2  # Base score by weapon power

        # Healing: higher when HP low
        elif type_id in RECOVERY_ITEMS and RECOVERY_ITEMS.get(type_id, 0) > 0:
            if my_hp < HP_CRITICAL_THRESHOLD:
                score += 18
            elif my_hp < HP_MODERATE_THRESHOLD:
                score += 12
            else:
                score += 6

        # Energy drink: higher when EP low
        elif type_id == "energy_drink":
            if ep <= 3:
                score += 15
            elif ep <= 6:
                score += 10
            else:
                score += 5

        # Moltz / currency
        elif type_id == "rewards" or category == "currency":
            score += 15

        # Utility
        elif type_id in ("binoculars", "map"):
            score += 10

        item_region_scores[rid] = item_region_scores.get(rid, 0) + score

    enemy_region_count = {}
    if visible_agents:
        for a in visible_agents:
            if isinstance(a, dict) and a.get("isAlive") and a.get("id") != my_id:
                rid = a.get("regionId", "")
                if rid:
                    enemy_region_count[rid] = enemy_region_count.get(rid, 0) + 1

    enemies = [a for a in visible_agents if isinstance(a, dict) and not a.get("isGuardian") and a.get("isAlive") and a.get("id") != my_id]

    # Build set of directly connected region IDs
    connected_ids = set()
    for conn in connections:
        connected_ids.add(conn if isinstance(conn, str) else conn.get("id", ""))

    # Distant item attraction: items visible but not in adjacent regions
    # Use visibleRegions to find which connected region is on the path
    distant_direction_bonus = {}
    if visible_regions:
        for rid, score in item_region_scores.items():
            if rid in connected_ids or score <= 0:
                continue  # Already handled as adjacent, or no score
            # Find the region object in visibleRegions
            item_region = None
            for vr in visible_regions:
                if isinstance(vr, dict) and vr.get("id") == rid:
                    item_region = vr
                    break
            if not item_region:
                continue
            # Check which of our connected regions also connects to the item region
            for ic in item_region.get("connections", []):
                ic_id = ic if isinstance(ic, str) else ic.get("id", "")
                if ic_id and ic_id in connected_ids:
                    # Moving to this connected region gets us closer
                    distant_direction_bonus[ic_id] = distant_direction_bonus.get(ic_id, 0) + score * 0.4

    for conn in connections:
        if isinstance(conn, str):
            # HARD BLOCK: never move into danger zone
            if conn in danger_ids:
                continue
            score = 1
            score += item_region_scores.get(conn, 0)
            score += distant_direction_bonus.get(conn, 0)
            
            # VISITED REGION PENALTY
            is_new = conn not in _visited_regions
            if not is_new:
                score -= visited_penalty
            else:
                score += new_region_bonus

            candidates.append({
                "id": conn,
                "name": conn[:8],
                "score": score,
                "enemies": enemy_region_count.get(conn, 0),
                "intel": [],
                "is_new": is_new
            })

        elif isinstance(conn, dict):
            rid = conn.get("id", "")
            # HARD BLOCK: never move into DZ or pending DZ
            if not rid or conn.get("isDeathZone") or rid in danger_ids:
                continue

            # Scoring
            score = 0
            enemy_count = enemy_region_count.get(rid, 0)

            # 1. RESOLVE REGION & TERRAIN
            resolved = _resolve_region(conn, {"visibleRegions": visible_regions})
            terrain = resolved.get("terrain", "").lower() if resolved else conn.get("terrain", "").lower()
            terrain_scores = {
                "hills": 4, "plains": 2, "ruins": 2,
                "forest": 1, "water": -3,
            }
            score += terrain_scores.get(terrain, 0)

            # 2. ITEMS
            score += item_region_scores.get(rid, 0)
            score += distant_direction_bonus.get(rid, 0)

            # Facilities attract
            facs = conn.get("interactables", [])
            if facs:
                unused = [f for f in facs if isinstance(f, dict) and not f.get("isUsed")]
                score += len(unused) * 2

            # 3. WEATHER
            weather = conn.get("weather", "").lower()
            weather_penalty = {"storm": -2, "fog": -1, "rain": 0, "clear": 1}
            score += weather_penalty.get(weather, 0)
            if weather == "storm" and terrain in ("forest", "ruins"): score += 5
            if alive_count <= 5 and terrain in ("ruins", "forest"): score += 10

            # 3. SCOUT MODE & WEAPON SPECIALIZATION
            if terrain == "hills":
                score += 15
                log.debug("SCOUT_MODE: Favoring hills for better vision")

            if has_weapon:
                if w_type == "sniper" and terrain == "hills": score += 15
                elif w_type in ("katana", "sword") and terrain in ("forest", "ruins"): score += 10
                elif terrain == "plains": score -= 5 

            # 4. ENEMY ATTRACTION (Hunter / Steal Kill Logic)
            # HARD BLOCK: Never move into high enemy zones if not ready for war
            MAX_SAFE_ENEMIES = 3 if is_ready_for_war else 1
            if enemy_count > MAX_SAFE_ENEMIES and not is_ready_for_war and my_hp < 60:
                log.warning("🚫 SCAN: %s has %d enemies, not safe! Skipping.", resolved.get("name", rid)[:8], enemy_count)
                continue  # Skip this region entirely
            
            if enemy_count > 0:
                if my_hp < 40:
                    score -= enemy_count * 30  # Increased penalty
                elif is_ready_for_war:
                    score += enemy_count * 25
                    if enemy_count >= 2:
                        score += 50  # Increased Steal Kill bonus
                        log.info("🔥 HOT_ZONE: Multiple enemies in %s - MOVING FOR STEAL KILL!", resolved.get("name", rid))
                elif AGGRESSION_LEVEL == "aggressive":
                    score += enemy_count * 10
                else:
                    score -= enemy_count * 15  # Stronger penalty for unknown danger

            # 5. EXPLORATION vs BACKTRACKING vs SAFE EXIT
            # IMPROVED: Safe exit routes more important than "new region" when HP low
            conns = conn.get("connections", []) if isinstance(conn, dict) else []
            exit_options = len(conns)
            
            if rid in _visited_regions:
                score -= visited_penalty
            else:
                score += new_region_bonus
            
            # IMPROVED: Bonus for regions with more exit options (safe retreat)
            if my_hp < 50 or is_late_game:
                # When vulnerable, prioritize regions with multiple exits
                exit_bonus = exit_options * 8  # Up to +24 for 3 exits
                score += exit_bonus
                if exit_bonus > 0:
                    log.debug("SAFE_EXIT: %s has %d exits, bonus=%d", rid[:8], exit_options, exit_bonus)
            else:
                # Normal mode: moderate bonus for exits
                score += exit_options * 3

            # 6. GUARDIAN HUNTING & CENTER POSITIONING
            if rid in _guardian_locations: score += 15
            
            # IMPROVED: Late game center vs edge strategy
            is_in_center = (_map_knowledge.get("revealed") and 
                           rid in _map_knowledge.get("safe_center", []))
            
            if is_in_center:
                if is_endgame:
                    # Endgame: CENTER is crucial - more connections, harder to be trapped
                    score += 40  # Strong center bias
                    log.debug("ENDGAME_CENTER: %s is center region, +40", rid[:8])
                elif is_late_game:
                    # Late game: prefer center for better positioning
                    score += 20
                else:
                    # Early/mid: moderate center preference
                    score += 5
            elif is_late_game and _map_knowledge.get("revealed"):
                # Late game: EDGE regions are riskier (easier to be trapped by DZ)
                # Check if this edge region has good escape routes
                if exit_options <= 1:
                    score -= 25  # Dead end in late game = dangerous
                    log.warning("EDGE_DEADEND: %s is edge with %d exits, -25", rid[:8], exit_options)
                else:
                    score -= 5  # Slight edge penalty
            
            if rid in _map_knowledge.get("death_zones", set()):
                continue  # HARD BLOCK

            # WEAPON RANGE POSITIONING: maintain optimal range for ranged weapons
            if equipped:
                w_range = get_weapon_range(equipped)
                if w_range >= 1 and enemy_count > 0:
                    # If we have a gun, we PREFER to stay 1 region away from enemies
                    # instead of moving into their region.
                    score -= 10  # Penalty for moving into melee range with a gun
                    log.debug("RANGED_POSITIONING: Avoiding melee range for region %s", rid[:8])
                elif w_range >= 1 and any(enemy_region_count.get(adj, 0) > 0 for adj in _get_adjacent_ids(conn, visible_regions)):
                    score += 5  # Bonus for staying at range
            
            # ENEMY SCAN: Detailed intel for the log
            enemy_intel = []
            if enemy_count > 0:
                for e in enemies:
                    if e.get("regionId") == rid:
                        e_hp = e.get("hp", "?")
                        e_wpn = e.get("equippedWeapon", {}).get("typeId", "fist") if isinstance(e.get("equippedWeapon"), dict) else "fist"
                        enemy_intel.append(f"HP:{e_hp}/W:{e_wpn}")

            candidates.append({
                "id": rid,
                "name": resolved.get("name", "Unknown"),
                "score": score,
                "enemies": enemy_count,
                "intel": enemy_intel,
                "is_new": rid not in _visited_regions
            })

    if not candidates:
        log.debug("MOVE: No valid candidates from %d connections", len(connections))
        return None

    # SORT BY SCORE (Highest first)
    candidates.sort(key=lambda x: x["score"], reverse=True)

    # Log Move Radar for visual feedback
    log.info("--- MOVE_RADAR (Top 3 Candidates) ---")
    for c in candidates[:3]:
        status = "🔥 HOT" if c["enemies"] >= 2 else ("👤 HUNT" if c["enemies"] == 1 else "🗺️ EXPLORE")
        intel_str = f" | Intel: {', '.join(c['intel'])}" if c["intel"] else ""
        new_tag = " ✨" if c["is_new"] else ""
        log.info(f"  [{c['score']} pts] {status} -> {c['name']}{new_tag}{intel_str}")
    # Final Choice
    return candidates[0]["id"]

"""
View fields from api-summary.md (all implemented above — v1.5.2):
✅ self          — hp, ep, atk, def, inventory, equippedWeapon, isAlive
✅ currentRegion — id, name, terrain, weather, connections, interactables, isDeathZone
✅ connectedRegions — full Region objects OR bare string IDs (type-safe via _resolve_region)
✅ visibleRegions  — used for connectedRegions fallback + region ID lookup
✅ visibleAgents   — guardians (HOSTILE!) + enemies + combat targeting
✅ visibleMonsters — monster farming targets
✅ visibleNPCs     — acknowledged (NPCs are flavor per game-systems.md)
✅ visibleItems    — pickup + movement attraction scoring
✅ pendingDeathzones — {id, name} entries for death zone escape + movement planning
✅ recentLogs      — available for analysis
✅ recentMessages  — communication (curse disabled in v1.5.2)
✅ aliveCount      — adaptive aggression (late game adjustment)
"""
