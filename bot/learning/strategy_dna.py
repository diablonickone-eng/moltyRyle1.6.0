"""
Self-Learning Strategy DNA + Evolution Engine
Bot learns from match outcomes and auto-tunes parameters
"""
import json
import random
import os
from datetime import datetime
from typing import Dict, Any, List
from bot.utils.logger import get_logger

log = get_logger(__name__)

# Default Strategy DNA - initial gene pool
DEFAULT_DNA = {
    # Combat thresholds (genes)
    "combat_hp_threshold": 50,       # Min HP untuk fight
    "finisher_threshold_early": 40,  # HP musuh untuk finisher (early)
    "finisher_threshold_late": 60,   # HP musuh untuk finisher (late)
    "ready_for_war_hp": 60,          # HP threshold untuk "war mode"
    
    # Aggression curve per game phase
    "aggression_early": 0.3,         # 0-1 (30% aggressive)
    "aggression_mid": 0.6,           # 0-1 (60% aggressive)
    "aggression_late": 0.9,          # 0-1 (90% aggressive)
    
    # Item priorities (genes)
    "weapon_priority_boost": 100,    # Base score for weapons
    "heal_stockpile_target": 4,      # Target healing items
    "currency_priority": 300,        # Moltz priority
    
    # Movement weights
    "exploration_weight": 10,        # Score for unvisited regions
    "enemy_avoidance_weight": 20,    # Penalty for enemy regions
    "loot_proximity_weight": 15,     # Bonus for nearby loot
    "hunting_weight": 50,            # Bonus for hunting enemies
    
    # Risk tolerance
    "max_enemies_safe": 2,           # Max enemies untuk dianggap "safe"
    "danger_flee_hp": 40,            # HP threshold untuk flee
    "chase_threshold_hp": 50,        # HP musuh untuk chase
}

# DNA save path
DNA_FILE = "data/strategy_dna.json"
MATCH_HISTORY_FILE = "data/match_history.json"


def _as_number(value, default=0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def sanitize_dna(raw_dna: Dict[str, Any]) -> Dict[str, Any]:
    """Merge DNA with defaults and enforce safe strategy bounds."""
    dna = DEFAULT_DNA.copy()
    if isinstance(raw_dna, dict):
        dna.update(raw_dna)

    int_bounds = {
        "combat_hp_threshold": (45, 100),
        "finisher_threshold_early": (20, 80),
        "finisher_threshold_late": (30, 90),
        "ready_for_war_hp": (55, 100),
        "weapon_priority_boost": (10, 500),
        "heal_stockpile_target": (1, 10),
        "currency_priority": (10, 500),
        "exploration_weight": (0, 100),
        "enemy_avoidance_weight": (0, 100),
        "loot_proximity_weight": (0, 100),
        "hunting_weight": (0, 100),
        "max_enemies_safe": (1, 4),
        "danger_flee_hp": (35, 100),
        "chase_threshold_hp": (20, 90),
    }
    float_bounds = {
        "aggression_early": (0.1, 0.8),
        "aggression_mid": (0.1, 0.9),
        "aggression_late": (0.2, 1.0),
    }

    for key, (minimum, maximum) in int_bounds.items():
        dna[key] = int(_clamp(_as_number(dna.get(key), DEFAULT_DNA[key]), minimum, maximum))
    for key, (minimum, maximum) in float_bounds.items():
        dna[key] = round(_clamp(_as_number(dna.get(key), DEFAULT_DNA[key]), minimum, maximum), 3)

    return dna


class StrategyDNA:
    """Genetic algorithm for strategy evolution"""
    
    def __init__(self):
        self.dna = self._load_dna()
        self.match_history: List[Dict] = self._load_history()
        self.generation = len(self.match_history)
        
    def _load_dna(self) -> Dict[str, Any]:
        """Load DNA from file or use default"""
        if os.path.exists(DNA_FILE):
            try:
                with open(DNA_FILE, 'r') as f:
                    return sanitize_dna(json.load(f))
            except:
                pass
        return sanitize_dna(DEFAULT_DNA)
    
    def _load_history(self) -> List[Dict]:
        """Load match history"""
        if os.path.exists(MATCH_HISTORY_FILE):
            try:
                with open(MATCH_HISTORY_FILE, 'r') as f:
                    return json.load(f)
            except:
                pass
        return []
    
    def save_dna(self):
        """Save current DNA to file"""
        os.makedirs(os.path.dirname(DNA_FILE), exist_ok=True)
        self.dna = sanitize_dna(self.dna)
        with open(DNA_FILE, 'w') as f:
            json.dump(self.dna, f, indent=2)
    
    def save_history(self):
        """Save match history"""
        os.makedirs(os.path.dirname(MATCH_HISTORY_FILE), exist_ok=True)
        with open(MATCH_HISTORY_FILE, 'w') as f:
            json.dump(self.match_history, f, indent=2)
    
    def get_gene(self, key: str) -> Any:
        """Get gene value"""
        return self.dna.get(key, DEFAULT_DNA.get(key))
    
    def record_match_result(self, result: Dict[str, Any]):
        """
        Record match outcome for learning
        
        result format:
        {
            "placement": 1-100,
            "kills": int,
            "survival_time": seconds,
            "damage_dealt": int,
            "damage_taken": int,
            "moltz_earned": int,
            "strategy_used": str,
            "dna_snapshot": dict  # DNA used in this match
        }
        """
        clean_result = {
            **result,
            "placement": int(_as_number(result.get("placement", 100), 100)),
            "kills": int(_as_number(result.get("kills", 0), 0)),
            "survival_time": int(_as_number(result.get("survival_time", 0), 0)),
            "damage_dealt": int(_as_number(result.get("damage_dealt", 0), 0)),
            "damage_taken": int(_as_number(result.get("damage_taken", 0), 0)),
            "moltz_earned": int(_as_number(result.get("moltz_earned", 0), 0)),
            "dna_snapshot": sanitize_dna(result.get("dna_snapshot", self.dna)),
            # NEW: Detailed analytics fields
            "cause_of_death": result.get("cause_of_death"),
            "time_of_death": result.get("time_of_death"),
            "last_region_id": result.get("last_region_id"),
            "items_used": result.get("items_used", []),
            "heal_items_used": int(_as_number(result.get("heal_items_used", 0), 0)),
            "weapon_switches": int(_as_number(result.get("weapon_switches", 0), 0)),
            "facilities_used": result.get("facilities_used", []),
            "peak_hp": int(_as_number(result.get("peak_hp", 100), 100)),
            "lowest_hp": int(_as_number(result.get("lowest_hp", 100), 100)),
            "total_moves": int(_as_number(result.get("total_moves", 0), 0)),
            "total_rests": int(_as_number(result.get("total_rests", 0), 0)),
        }
        entry = {
            "timestamp": datetime.now().isoformat(),
            "generation": self.generation,
            **clean_result
        }
        entry["fitness"] = round(self.calculate_fitness(entry), 2)
        self.match_history.append(entry)
        self.save_history()
        self.save_dna()
        
        # Evolve after collecting enough data
        if len(self.match_history) >= 5:
            self._evolve()
    
    def calculate_fitness(self, match: Dict) -> float:
        """
        Calculate fitness score for a match
        Higher = better strategy
        
        NEW: Includes moltz_earned (economic efficiency) and damage_taken (survival quality)
        """
        placement = _as_number(match.get("placement", 100), 100)
        kills = _as_number(match.get("kills", 0), 0)
        survival_time = _as_number(match.get("survival_time", 0), 0)
        damage_dealt = _as_number(match.get("damage_dealt", 0), 0)
        damage_taken = _as_number(match.get("damage_taken", 0), 0)
        moltz_earned = _as_number(match.get("moltz_earned", 0), 0)
        
        # Base fitness formula
        fitness = (
            (101 - placement) * 10 +        # Placement (win = 1000 pts)
            kills * 100 +                   # Kills (100 pts each)
            survival_time * 0.5 +           # Survival (0.5 pts/sec)
            damage_dealt * 0.1 +            # Damage dealt (0.1 pts/dmg)
            moltz_earned * 0.5              # Moltz earned (0.5 pts/moltz - economic efficiency)
        )
        
        # PENALTY for damage taken (inefficient trading)
        # High damage_taken means bot took too much risk or traded poorly
        damage_efficiency = damage_dealt / max(damage_taken, 1)  # Avoid div by zero
        if damage_efficiency < 1.0:  # Took more damage than dealt
            fitness -= (damage_taken - damage_dealt) * 0.05  # Penalty for bad trades
        
        return max(0, fitness)  # Ensure non-negative
    
    def _evolve(self):
        """
        Genetic evolution - improved algorithm with:
        - 10-20 recent matches window
        - Differential scoring (match-to-match improvement)
        - Anti-outlier protection (requires consensus, not single outlier)
        """
        # IMPROVED: Use last 10-20 matches for better statistical significance
        MATCH_WINDOW = min(20, max(10, len(self.match_history) // 3))
        recent_matches = self.match_history[-MATCH_WINDOW:]
        
        if len(recent_matches) < 10:
            return  # Not enough data for reliable evolution
        
        # Calculate fitness for all recent matches
        fitness_scores = [self.calculate_fitness(m) for m in recent_matches]
        avg_fitness = sum(fitness_scores) / len(fitness_scores)
        
        # ANTI-OUTLIER: Use median and std dev to detect outliers
        sorted_fitness = sorted(fitness_scores)
        median_fitness = sorted_fitness[len(sorted_fitness) // 2]
        std_dev = (sum((f - avg_fitness) ** 2 for f in fitness_scores) / len(fitness_scores)) ** 0.5
        
        log.info("🧬 EVOLUTION: Gen %d | Window=%d | Avg=%.1f | Median=%.1f | Std=%.1f",
                 self.generation, MATCH_WINDOW, avg_fitness, median_fitness, std_dev)
        
        # DIFFERENTIAL: Compare with previous generation's performance
        if len(self.match_history) > MATCH_WINDOW:
            older_matches = self.match_history[-(MATCH_WINDOW*2):-MATCH_WINDOW]
            older_fitness = [self.calculate_fitness(m) for m in older_matches]
            older_avg = sum(older_fitness) / len(older_fitness)
            improvement = avg_fitness - older_avg
            log.info("🧬 DIFFERENTIAL: Previous gen avg=%.1f | Improvement=%+.1f", 
                     older_avg, improvement)
        else:
            improvement = 0
        
        # ANTI-OUTLIER: Only consider matches within 1.5 std dev of median
        valid_matches = [
            m for m, f in zip(recent_matches, fitness_scores)
            if abs(f - median_fitness) <= 1.5 * std_dev
        ]
        
        if len(valid_matches) >= 5:
            # Use consensus from valid matches, not single outlier
            valid_fitness = [self.calculate_fitness(m) for m in valid_matches]
            valid_avg = sum(valid_fitness) / len(valid_fitness)
            
            # Get best match from VALID set only (not outlier)
            best_valid = max(valid_matches, key=self.calculate_fitness)
            best_fitness = self.calculate_fitness(best_valid)
            
            # Only adopt if significantly better than current AND improvement trend
            if best_fitness > valid_avg * 1.15 and improvement >= -50:
                best_dna = sanitize_dna(best_valid.get("dna_snapshot", self.dna))
                log.info("🧬 CONSENSUS ADOPT: Best valid fitness=%.1f (avg=%.1f)", 
                         best_fitness, valid_avg)
                self.dna = best_dna.copy()
            else:
                # Guided mutation based on trend direction
                if improvement < 0:
                    # Declining performance - more aggressive mutation
                    log.info("🧬 DECLINING - aggressive mutation needed")
                    self._mutate(avg_fitness, aggressive=True)
                else:
                    # Stable or improving - conservative mutation
                    self._mutate(avg_fitness, aggressive=False)
        else:
            log.info("🧬 INSUFFICIENT VALID MATCHES - skipping evolution")
        
        self.generation += 1
        self.save_dna()
    
    def _mutate(self, current_fitness: float, aggressive: bool = False):
        """
        Random mutation of DNA genes
        
        IMPROVED: Supports aggressive mode for declining performance
        """
        # Aggressive mode: higher rate and strength when performance declining
        mutation_rate = 0.25 if aggressive else 0.1  # 25% vs 10% chance
        mutation_strength = 0.35 if aggressive else 0.2  # +/- 35% vs 20%
        
        mutations = []
        
        for key, value in self.dna.items():
            if random.random() < mutation_rate:
                if isinstance(value, (int, float)):
                    # Numeric mutation
                    change = 1 + random.uniform(-mutation_strength, mutation_strength)
                    new_value = value * change
                    
                    # Keep within bounds
                    if key.endswith("_hp") or key.endswith("threshold"):
                        new_value = max(10, min(100, new_value))  # HP bounds
                    elif "priority" in key:
                        new_value = max(10, min(500, new_value))  # Priority bounds
                    elif "aggression" in key:
                        new_value = max(0.1, min(1.0, new_value))  # 0-1 bounds
                    
                    if isinstance(value, int):
                        new_value = int(new_value)
                    
                    self.dna[key] = new_value
                    mutations.append(f"{key}: {value:.1f} → {new_value:.1f}")
        
        self.dna = sanitize_dna(self.dna)

        if mutations:
            mode_str = "AGGRESSIVE" if aggressive else "conservative"
            log.info("🧬 %s MUTATIONS (%s): %s", mode_str.upper(), 
                    f"rate={mutation_rate}, strength={mutation_strength}",
                    " | ".join(mutations[:5]))  # Show max 5 mutations
        else:
            log.info("🧬 No mutations this generation")
    
    def get_strategy_params(self, game_phase: str, hp: int, alive_count: int) -> Dict:
        """
        Get strategy parameters for current game state
        Auto-adjusts based on learned DNA
        """
        # Determine aggression level from DNA
        if game_phase == "early":
            aggression = self.get_gene("aggression_early")
            finisher_threshold = self.get_gene("finisher_threshold_early")
        elif game_phase == "mid":
            aggression = self.get_gene("aggression_mid")
            finisher_threshold = (self.get_gene("finisher_threshold_early") + 
                               self.get_gene("finisher_threshold_late")) / 2
        else:  # late
            aggression = self.get_gene("aggression_late")
            finisher_threshold = self.get_gene("finisher_threshold_late")
        
        return {
            "combat_hp_threshold": self.get_gene("combat_hp_threshold"),
            "finisher_threshold": int(finisher_threshold),
            "ready_for_war_hp": self.get_gene("ready_for_war_hp"),
            "aggression": aggression,
            "max_enemies_safe": self.get_gene("max_enemies_safe"),
            "chase_threshold_hp": self.get_gene("chase_threshold_hp"),
            "should_hunt": aggression > 0.7 or alive_count < 20,
            "should_avoid": aggression < 0.3 and hp < 50,
        }


# Global DNA instance
_dna = StrategyDNA()

def get_dna() -> StrategyDNA:
    """Get global DNA instance"""
    return _dna


def record_match(placement: int, kills: int, survival_time: int, 
               damage_dealt: int, damage_taken: int, moltz: int = 0,
               # NEW: Detailed analytics parameters
               cause_of_death: str = None,
               time_of_death = None,
               last_region_id: str = None,
               items_used: list = None,
               heal_items_used: int = 0,
               weapon_switches: int = 0,
               facilities_used: list = None,
               peak_hp: int = 100,
               lowest_hp: int = 100,
               total_moves: int = 0,
               total_rests: int = 0):
    """Convenience function to record match result with detailed analytics"""
    dna = get_dna()
    dna.record_match_result({
        "placement": placement,
        "kills": kills,
        "survival_time": survival_time,
        "damage_dealt": damage_dealt,
        "damage_taken": damage_taken,
        "moltz_earned": moltz,
        "dna_snapshot": dna.dna.copy(),
        # NEW: Detailed analytics
        "cause_of_death": cause_of_death,
        "time_of_death": time_of_death,
        "last_region_id": last_region_id,
        "items_used": items_used or [],
        "heal_items_used": heal_items_used,
        "weapon_switches": weapon_switches,
        "facilities_used": facilities_used or [],
        "peak_hp": peak_hp,
        "lowest_hp": lowest_hp,
        "total_moves": total_moves,
        "total_rests": total_rests,
    })


if __name__ == "__main__":
    # Test evolution
    dna = StrategyDNA()
    
    # Simulate some matches
    for i in range(5):
        record_match(
            placement=random.randint(1, 50),
            kills=random.randint(0, 5),
            survival_time=random.randint(100, 1000),
            damage_dealt=random.randint(50, 500),
            damage_taken=random.randint(20, 200)
        )
    
    print("Current DNA:", json.dumps(dna.dna, indent=2))
