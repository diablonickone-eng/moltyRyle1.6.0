"""
Autonomous Agentic AI System - Self-Directed Bot Development
Implements continuous improvement without human intervention
"""
import asyncio
import json
import time
from typing import Dict, List, Any
from dataclasses import dataclass
from bot.utils.logger import get_logger

log = get_logger(__name__)

@dataclass
class PerformanceMetrics:
    """Track bot performance metrics for autonomous optimization"""
    kills_per_game: float = 0.0
    deaths_per_game: float = 0.0
    kd_ratio: float = 0.0
    survival_time: float = 0.0
    weapons_acquired: float = 0.0
    guardians_killed: float = 0.0
    ep_efficiency: float = 0.0
    combat_success_rate: float = 0.0

@dataclass
class StrategyParameters:
    """Configurable strategy parameters for autonomous tuning"""
    aggression_level: float = 0.7  # 0.0 (defensive) to 1.0 (aggressive)
    weak_enemy_threshold: int = 60  # HP threshold for easy kills
    range_combat_priority: float = 0.8  # Priority for range advantage
    guardian_hunting_priority: float = 0.6  # Priority for guardian farming
    ep_conservation_threshold: int = 8  # EP threshold for conservative play
    risk_tolerance: float = 0.5  # Risk tolerance in combat decisions

class AutonomousAgenticAI:
    """Self-directed AI system for continuous bot optimization"""
    
    def __init__(self):
        self.metrics = PerformanceMetrics()
        self.strategy_params = StrategyParameters()
        self.performance_history: List[PerformanceMetrics] = []
        self.optimization_cycle = 0
        self.last_optimization_time = time.time()
        
    async def analyze_performance(self, game_data: Dict[str, Any]) -> PerformanceMetrics:
        """Analyze recent game performance and extract metrics"""
        try:
            # Extract performance data from game logs/results
            metrics = PerformanceMetrics()
            
            # Calculate combat metrics
            metrics.kills_per_game = game_data.get('kills', 0)
            metrics.deaths_per_game = 1 if game_data.get('is_dead', False) else 0
            metrics.kd_ratio = metrics.kills_per_game / max(1, metrics.deaths_per_game)
            
            # Calculate survival time
            metrics.survival_time = game_data.get('survival_time', 0)
            
            # Track resource acquisition
            metrics.weapons_acquired = len(game_data.get('weapons_pickedup', []))
            metrics.guardians_killed = game_data.get('guardians_killed', 0)
            
            # Calculate EP efficiency
            ep_consumed = game_data.get('ep_consumed', 0)
            ep_recovered = game_data.get('ep_recovered', 0)
            metrics.ep_efficiency = ep_recovered / max(1, ep_consumed)
            
            # Combat success rate
            combats_attempted = game_data.get('combats_attempted', 0)
            combats_won = game_data.get('combats_won', 0)
            metrics.combat_success_rate = combats_won / max(1, combats_attempted)
            
            log.info("🤖 Autonomous AI: Performance analysis complete")
            log.info("   K/D Ratio: %.2f | Survival: %.0fs | EP Efficiency: %.2f", 
                    metrics.kd_ratio, metrics.survival_time, metrics.ep_efficiency)
            
            return metrics
            
        except Exception as e:
            log.error("🤖 Autonomous AI: Performance analysis failed: %s", e)
            return self.metrics
    
    async def optimize_strategy(self, current_metrics: PerformanceMetrics) -> StrategyParameters:
        """Autonomously optimize strategy parameters based on performance"""
        try:
            new_params = StrategyParameters()
            
            # Analyze performance trends
            if len(self.performance_history) >= 3:
                recent_avg = self.calculate_recent_average()
                
                # Optimize aggression based on K/D ratio
                if current_metrics.kd_ratio < recent_avg.kd_ratio * 0.8:
                    # Performance declining - reduce aggression
                    new_params.aggression_level = max(0.3, self.strategy_params.aggression_level * 0.9)
                    log.info("🤖 Autonomous AI: Reducing aggression to %.2f (K/D decline)", 
                            new_params.aggression_level)
                elif current_metrics.kd_ratio > recent_avg.kd_ratio * 1.2:
                    # Performance improving - increase aggression
                    new_params.aggression_level = min(1.0, self.strategy_params.aggression_level * 1.1)
                    log.info("🤖 Autonomous AI: Increasing aggression to %.2f (K/D improvement)", 
                            new_params.aggression_level)
                else:
                    new_params.aggression_level = self.strategy_params.aggression_level
                
                # Optimize EP management
                if current_metrics.ep_efficiency < 0.5:
                    new_params.ep_conservation_threshold = self.strategy_params.ep_conservation_threshold + 2
                    log.info("🤖 Autonomous AI: Increasing EP conservation threshold to %d", 
                            new_params.ep_conservation_threshold)
                elif current_metrics.ep_efficiency > 0.8:
                    new_params.ep_conservation_threshold = max(5, self.strategy_params.ep_conservation_threshold - 1)
                    log.info("🤖 Autonomous AI: Decreasing EP conservation threshold to %d", 
                            new_params.ep_conservation_threshold)
                
                # Optimize weak enemy threshold
                if current_metrics.combat_success_rate > 0.7:
                    new_params.weak_enemy_threshold = min(80, self.strategy_params.weak_enemy_threshold + 5)
                    log.info("🤖 Autonomous AI: Increasing weak enemy threshold to %d", 
                            new_params.weak_enemy_threshold)
                elif current_metrics.combat_success_rate < 0.4:
                    new_params.weak_enemy_threshold = max(40, self.strategy_params.weak_enemy_threshold - 5)
                    log.info("🤖 Autonomous AI: Decreasing weak enemy threshold to %d", 
                            new_params.weak_enemy_threshold)
                
            else:
                # Not enough data - use current parameters
                new_params = self.strategy_params
            
            self.optimization_cycle += 1
            self.last_optimization_time = time.time()
            
            log.info("🤖 Autonomous AI: Strategy optimization complete (Cycle %d)", 
                    self.optimization_cycle)
            
            return new_params
            
        except Exception as e:
            log.error("🤖 Autonomous AI: Strategy optimization failed: %s", e)
            return self.strategy_params
    
    def calculate_recent_average(self) -> PerformanceMetrics:
        """Calculate average of recent performance metrics"""
        if not self.performance_history:
            return PerformanceMetrics()
        
        recent = self.performance_history[-3:]  # Last 3 games
        avg = PerformanceMetrics()
        
        avg.kills_per_game = sum(m.kills_per_game for m in recent) / len(recent)
        avg.deaths_per_game = sum(m.deaths_per_game for m in recent) / len(recent)
        avg.kd_ratio = sum(m.kd_ratio for m in recent) / len(recent)
        avg.survival_time = sum(m.survival_time for m in recent) / len(recent)
        avg.weapons_acquired = sum(m.weapons_acquired for m in recent) / len(recent)
        avg.guardians_killed = sum(m.guardians_killed for m in recent) / len(recent)
        avg.ep_efficiency = sum(m.ep_efficiency for m in recent) / len(recent)
        avg.combat_success_rate = sum(m.combat_success_rate for m in recent) / len(recent)
        
        return avg
    
    async def apply_strategy_updates(self, new_params: StrategyParameters) -> None:
        """Apply optimized strategy parameters to bot configuration"""
        try:
            # Update bot strategy parameters
            # This would integrate with the existing brain.py strategy system
            
            log.info("🤖 Autonomous AI: Applying strategy updates")
            log.info("   Aggression: %.2f | Weak Enemy Threshold: %d | EP Threshold: %d",
                    new_params.aggression_level, new_params.weak_enemy_threshold, 
                    new_params.ep_conservation_threshold)
            
            # Store new parameters
            self.strategy_params = new_params
            
            # Save to persistent storage for session continuity
            await self.save_optimization_state()
            
        except Exception as e:
            log.error("🤖 Autonomous AI: Failed to apply strategy updates: %s", e)
    
    async def save_optimization_state(self) -> None:
        """Save optimization state for session persistence"""
        try:
            state = {
                'strategy_params': {
                    'aggression_level': self.strategy_params.aggression_level,
                    'weak_enemy_threshold': self.strategy_params.weak_enemy_threshold,
                    'ep_conservation_threshold': self.strategy_params.ep_conservation_threshold,
                    'range_combat_priority': self.strategy_params.range_combat_priority,
                    'guardian_hunting_priority': self.strategy_params.guardian_hunting_priority,
                    'risk_tolerance': self.strategy_params.risk_tolerance,
                },
                'optimization_cycle': self.optimization_cycle,
                'last_optimization_time': self.last_optimization_time,
                'performance_history': [
                    {
                        'kills_per_game': m.kills_per_game,
                        'deaths_per_game': m.deaths_per_game,
                        'kd_ratio': m.kd_ratio,
                        'survival_time': m.survival_time,
                        'weapons_acquired': m.weapons_acquired,
                        'guardians_killed': m.guardians_killed,
                        'ep_efficiency': m.ep_efficiency,
                        'combat_success_rate': m.combat_success_rate,
                    }
                    for m in self.performance_history[-10:]  # Keep last 10 games
                ]
            }
            
            # Save to file for session persistence
            with open('d:\\AI game\\moltyRyle1.6.0\\autonomous_ai_state.json', 'w') as f:
                json.dump(state, f, indent=2)
            
            log.info("🤖 Autonomous AI: Optimization state saved")
            
            # 🤖 Cascade Integration: Report optimization to agentic AI
            try:
                optimization_report = {
                    'cycle': self.optimization_cycle,
                    'strategy_changes': {
                        'aggression_level': self.strategy_params.aggression_level,
                        'weak_enemy_threshold': self.strategy_params.weak_enemy_threshold,
                        'ep_conservation_threshold': self.strategy_params.ep_conservation_threshold,
                    },
                    'performance_summary': {
                        'recent_kd_ratio': self.calculate_recent_average().kd_ratio,
                        'games_analyzed': len(self.performance_history),
                        'last_optimization': self.last_optimization_time,
                    }
                }
                
                # Save optimization report for Cascade AI review
                with open('d:\\AI game\\moltyRyle1.6.0\\cascade_optimization_report.json', 'w') as f:
                    json.dump(optimization_report, f, indent=2)
                
                log.info("🤖 Cascade Integration: Optimization report generated for AI review")
                
            except Exception as e:
                log.warning("🤖 Cascade Integration: Failed to generate report: %s", e)
            
        except Exception as e:
            log.error("🤖 Autonomous AI: Failed to save optimization state: %s", e)
    
    async def load_optimization_state(self) -> None:
        """Load optimization state from previous session"""
        try:
            with open('d:\\AI game\\moltyRyle1.6.0\\autonomous_ai_state.json', 'r') as f:
                state = json.load(f)
            
            # Restore strategy parameters
            params_data = state.get('strategy_params', {})
            self.strategy_params.aggression_level = params_data.get('aggression_level', 0.7)
            self.strategy_params.weak_enemy_threshold = params_data.get('weak_enemy_threshold', 60)
            self.strategy_params.ep_conservation_threshold = params_data.get('ep_conservation_threshold', 8)
            self.strategy_params.range_combat_priority = params_data.get('range_combat_priority', 0.8)
            self.strategy_params.guardian_hunting_priority = params_data.get('guardian_hunting_priority', 0.6)
            self.strategy_params.risk_tolerance = params_data.get('risk_tolerance', 0.5)
            
            # Restore optimization state
            self.optimization_cycle = state.get('optimization_cycle', 0)
            self.last_optimization_time = state.get('last_optimization_time', time.time())
            
            # Restore performance history
            history_data = state.get('performance_history', [])
            self.performance_history = [
                PerformanceMetrics(**m) for m in history_data
            ]
            
            log.info("🤖 Autonomous AI: Optimization state loaded (Cycle %d)", 
                    self.optimization_cycle)
            
        except FileNotFoundError:
            log.info("🤖 Autonomous AI: No previous state found - starting fresh")
        except Exception as e:
            log.error("🤖 Autonomous AI: Failed to load optimization state: %s", e)
    
    async def run_autonomous_cycle(self, game_data: Dict[str, Any]) -> None:
        """Run complete autonomous optimization cycle"""
        try:
            log.info("🤖 Autonomous AI: Starting optimization cycle %d", 
                    self.optimization_cycle + 1)
            
            # 1. Analyze performance
            current_metrics = await self.analyze_performance(game_data)
            self.performance_history.append(current_metrics)
            
            # 2. Optimize strategy
            new_params = await self.optimize_strategy(current_metrics)
            
            # 3. Apply updates
            await self.apply_strategy_updates(new_params)
            
            log.info("🤖 Autonomous AI: Optimization cycle %d complete", 
                    self.optimization_cycle)
            
        except Exception as e:
            log.error("🤖 Autonomous AI: Optimization cycle failed: %s", e)

# Global autonomous AI instance
autonomous_ai = AutonomousAgenticAI()
