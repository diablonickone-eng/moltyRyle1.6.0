"""
Learning Dashboard - Visualize DNA evolution and performance
Run: python -m bot.learning.dashboard
"""
import json
import os
from datetime import datetime
from typing import List, Dict
from bot.learning.strategy_dna import DEFAULT_DNA, StrategyDNA

DATA_DIR = "data"
DNA_FILE = f"{DATA_DIR}/strategy_dna.json"
HISTORY_FILE = f"{DATA_DIR}/match_history.json"


def load_json(filepath: str):
    """Load JSON file"""
    if not os.path.exists(filepath):
        return {}
    with open(filepath, 'r') as f:
        return json.load(f)


def format_number(val, decimals=2):
    """Format number for display"""
    if isinstance(val, (int, float)):
        return f"{val:.{decimals}f}" if isinstance(val, float) else str(val)
    return str(val)


def as_number(value, default=0):
    """Coerce persisted JSON values to a number for dashboard math."""
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value, default=0):
    return int(as_number(value, default))


def print_header(text: str, char="="):
    """Print section header"""
    width = 60
    print(char * width)
    print(f" {text}".center(width))
    print(char * width)


def show_current_dna():
    """Display current DNA genes"""
    dna = load_json(DNA_FILE)
    if not dna:
        print("No saved DNA yet. Showing default strategy DNA until 5+ matches trigger evolution.")
        dna = DEFAULT_DNA.copy()
    
    if not dna:
        print("❌ No DNA found. Run some matches first!")
        return
    
    print_header("🧬 CURRENT STRATEGY DNA", "=")
    
    categories = {
        "Combat Thresholds": ["combat_hp_threshold", "finisher_threshold_early", 
                              "finisher_threshold_late", "ready_for_war_hp"],
        "Aggression Curve": ["aggression_early", "aggression_mid", "aggression_late"],
        "Item Priorities": ["weapon_priority_boost", "heal_stockpile_target", "currency_priority"],
        "Movement Weights": ["exploration_weight", "enemy_avoidance_weight", 
                            "loot_proximity_weight", "hunting_weight"],
        "Risk Tolerance": ["max_enemies_safe", "danger_flee_hp", "chase_threshold_hp"],
    }
    
    for cat, keys in categories.items():
        print(f"\n📁 {cat}")
        print("-" * 40)
        for key in keys:
            if key in dna:
                val = dna[key]
                bar = "█" * int(val / 5) if isinstance(val, (int, float)) else ""
                print(f"  {key:25} = {format_number(val):8} {bar}")
    
    print()


def show_match_history():
    """Display match history and trends"""
    history = load_json(HISTORY_FILE)
    
    if not history:
        print("❌ No match history found!")
        return
    
    print_header("📊 MATCH HISTORY", "=")
    
    # Summary stats
    placements = [as_number(m.get("placement", 100), 100) for m in history]
    kills = [as_number(m.get("kills", 0), 0) for m in history]
    survival = [as_number(m.get("survival_time", 0), 0) for m in history]
    
    wins = sum(1 for p in placements if int(p) == 1)
    top10 = sum(1 for p in placements if p <= 10)
    
    print(f"\n📈 PERFORMANCE SUMMARY")
    print("-" * 40)
    print(f"  Total Matches:     {len(history)}")
    print(f"  Wins (#1):         {wins} ({wins/len(history)*100:.1f}%)")
    print(f"  Top 10:            {top10} ({top10/len(history)*100:.1f}%)")
    print(f"  Avg Placement:     {sum(placements)/len(placements):.1f}")
    print(f"  Avg Kills:         {sum(kills)/len(kills):.1f}")
    print(f"  Avg Survival:      {sum(survival)/len(survival):.0f}s")
    print(f"  Total Kills:       {as_int(sum(kills), 0)}")
    
    # Recent matches
    print(f"\n📋 RECENT MATCHES (Last 10)")
    print("-" * 70)
    print(f"{'#':<4} {'Time':<20} {'Place':<8} {'Kills':<8} {'Survived':<12} {'Fitness':<10}")
    print("-" * 70)
    
    for i, match in enumerate(reversed(history[-10:]), 1):
        ts = match.get("timestamp", "?")[:16]
        place = as_int(match.get("placement", 100), 100)
        kills = as_int(match.get("kills", 0), 0)
        surv = as_int(match.get("survival_time", 0), 0)
        fitness = match.get("fitness")
        if fitness is None:
            fitness = StrategyDNA().calculate_fitness({
                "placement": place,
                "kills": kills,
                "survival_time": surv,
                "damage_dealt": as_number(match.get("damage_dealt", 0), 0),
            })
        else:
            fitness = as_number(fitness, 0)
        
        status = "👑" if place == 1 else "🔥" if place <= 10 else "💀" if place > 50 else " "
        survived = f"{surv}s"
        print(f"{i:<4} {ts:<20} {status} {place:<6} {kills:<8} {survived:<12} {fitness:<10.0f}")
    
    print()


def show_evolution_trends():
    """Show how DNA evolved over time"""
    history = load_json(HISTORY_FILE)
    
    if len(history) < 3:
        print("❌ Need at least 3 matches to show evolution trends!")
        return
    
    print_header("🧬 EVOLUTION TRENDS", "=")
    
    # Track key genes over time
    genes_to_track = ["combat_hp_threshold", "aggression_late", "finisher_threshold_late"]
    
    print(f"\n📈 GENE EVOLUTION (Last 20 matches)")
    print("-" * 70)
    print(f"{'Match':<8} {'Combat HP':<15} {'Aggression':<15} {'Finisher':<15}")
    print("-" * 70)
    
    for i, match in enumerate(history[-20:], len(history)-19):
        dna = match.get("dna_snapshot", {})
        
        combat_hp = dna.get("combat_hp_threshold", "-")
        agg = dna.get("aggression_late", "-")
        finisher = dna.get("finisher_threshold_late", "-")
        
        print(f"{i:<8} {format_number(combat_hp):<15} {format_number(agg):<15} {format_number(finisher):<15}")
    
    print()


def show_learning_recommendations():
    """AI recommendations based on performance"""
    history = load_json(HISTORY_FILE)
    
    if not history:
        return
    
    print_header("💡 AI RECOMMENDATIONS", "=")
    
    recent = history[-10:]
    avg_kills = sum(as_number(m.get("kills", 0), 0) for m in recent) / len(recent)
    avg_placement = sum(as_number(m.get("placement", 100), 100) for m in recent) / len(recent)
    
    recommendations = []
    
    if avg_kills < 1:
        recommendations.append("⚠️  LOW KILL COUNT → Increase aggression_late, lower finisher_threshold")
        recommendations.append("   Bot might be too passive. Try hunting more enemies.")
    elif avg_kills > 3:
        recommendations.append("✅ GOOD KILL COUNT → Strategy working well!")
    
    if avg_placement > 50:
        recommendations.append("⚠️  LOW SURVIVAL → Increase danger_flee_hp, be more defensive")
        recommendations.append("   Bot dying too early. Focus on survival first.")
    elif avg_placement < 10:
        recommendations.append("✅ GREAT PLACEMENT → Keep current strategy!")
    
    if not recommendations:
        recommendations.append("ℹ️  Continue gathering data for better recommendations...")
    
    for rec in recommendations:
        print(f"\n{rec}")
    
    print()


def main():
    """Main dashboard"""
    os.system('cls' if os.name == 'nt' else 'clear')
    
    print("\n" + "=" * 60)
    print("🤖 MOLTY BOT - SELF-LEARNING DASHBOARD".center(60))
    print("=" * 60)
    
    try:
        show_current_dna()
        show_match_history()
        show_evolution_trends()
        show_learning_recommendations()
    except Exception as e:
        print(f"❌ Error: {e}")
    
    print("\n" + "=" * 60)
    print("Press Enter to refresh, Ctrl+C to exit")
    input()


if __name__ == "__main__":
    while True:
        main()
