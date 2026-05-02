"""
Autonomous AI Integration - Self-Directed Bot Development System
Implements continuous improvement without human intervention
"""
import asyncio
import json
import time
from typing import Dict, Any
from bot.autonomous_ai import autonomous_ai
from bot.utils.logger import get_logger

log = get_logger(__name__)

class AutonomousBotManager:
    """Self-directed bot manager for continuous optimization"""
    
    def __init__(self):
        self.current_game_data = {}
        self.performance_tracking = True
        self.auto_optimization_enabled = True
        self.last_analysis_time = time.time()
        self.analysis_interval = 300  # 5 minutes between analyses
        
    async def initialize_autonomous_system(self):
        """Initialize autonomous AI system"""
        try:
            log.info("🤖 Autonomous System: Initializing...")
            
            # Load previous optimization state
            await autonomous_ai.load_optimization_state()
            
            # Enable performance tracking
            self.performance_tracking = True
            
            log.info("🤖 Autonomous System: Ready (Cycle %d)", 
                    autonomous_ai.optimization_cycle)
            
        except Exception as e:
            log.error("🤖 Autonomous System: Initialization failed: %s", e)
    
    async def track_game_performance(self, event_type: str, data: Dict[str, Any]):
        """Track game events for performance analysis"""
        try:
            if not self.performance_tracking:
                return
            
            # Store performance data
            if event_type == "game_ended":
                self.current_game_data.update(data)
                await self.analyze_and_optimize()
            elif event_type == "combat":
                self.current_game_data.setdefault('combats_attempted', 0)
                self.current_game_data.setdefault('combats_won', 0)
                self.current_game_data['combats_attempted'] += 1
                if data.get('won', False):
                    self.current_game_data['combats_won'] += 1
            elif event_type == "kill":
                self.current_game_data.setdefault('kills', 0)
                self.current_game_data['kills'] += 1
            elif event_type == "death":
                self.current_game_data['is_dead'] = True
            elif event_type == "weapon_pickup":
                self.current_game_data.setdefault('weapons_pickedup', [])
                weapon_type = data.get('weapon_type', 'unknown')
                self.current_game_data['weapons_pickedup'].append(weapon_type)
            elif event_type == "guardian_kill":
                self.current_game_data.setdefault('guardians_killed', 0)
                self.current_game_data['guardians_killed'] += 1
            elif event_type == "ep_consumed":
                self.current_game_data.setdefault('ep_consumed', 0)
                self.current_game_data['ep_consumed'] += data.get('amount', 0)
            elif event_type == "ep_recovered":
                self.current_game_data.setdefault('ep_recovered', 0)
                self.current_game_data['ep_recovered'] += data.get('amount', 0)
                
        except Exception as e:
            log.error("🤖 Autonomous System: Performance tracking failed: %s", e)
    
    async def analyze_and_optimize(self):
        """Analyze performance and optimize strategy automatically"""
        try:
            if not self.auto_optimization_enabled:
                return
            
            current_time = time.time()
            if current_time - self.last_analysis_time < self.analysis_interval:
                return
            
            log.info("🤖 Autonomous System: Starting performance analysis...")
            
            # Run autonomous optimization cycle
            await autonomous_ai.run_autonomous_cycle(self.current_game_data)
            
            # Reset for next game
            self.current_game_data = {}
            self.last_analysis_time = current_time
            
            log.info("🤖 Autonomous System: Analysis complete")
            
        except Exception as e:
            log.error("🤖 Autonomous System: Analysis failed: %s", e)
    
    async def get_current_strategy(self) -> Dict[str, Any]:
        """Get current strategy parameters for display"""
        try:
            params = autonomous_ai.strategy_params
            return {
                'aggression_level': params.aggression_level,
                'weak_enemy_threshold': params.weak_enemy_threshold,
                'ep_conservation_threshold': params.ep_conservation_threshold,
                'range_combat_priority': params.range_combat_priority,
                'guardian_hunting_priority': params.guardian_hunting_priority,
                'risk_tolerance': params.risk_tolerance,
                'optimization_cycle': autonomous_ai.optimization_cycle,
            }
        except Exception as e:
            log.error("🤖 Autonomous System: Failed to get strategy: %s", e)
            return {}
    
    async def force_optimization(self):
        """Force immediate optimization cycle"""
        try:
            log.info("🤖 Autonomous System: Force optimization requested")
            
            if self.current_game_data:
                await self.analyze_and_optimize()
            else:
                log.info("🤖 Autonomous System: No game data available for optimization")
                
        except Exception as e:
            log.error("🤖 Autonomous System: Force optimization failed: %s", e)
    
    async def enable_auto_optimization(self, enabled: bool = True):
        """Enable or disable automatic optimization"""
        self.auto_optimization_enabled = enabled
        log.info("🤖 Autonomous System: Auto optimization %s", 
                "enabled" if enabled else "disabled")
    
    async def get_performance_summary(self) -> Dict[str, Any]:
        """Get performance summary for monitoring"""
        try:
            if not autonomous_ai.performance_history:
                return {"status": "No performance data available"}
            
            recent = autonomous_ai.performance_history[-5:]  # Last 5 games
            avg_kd = sum(m.kd_ratio for m in recent) / len(recent)
            avg_survival = sum(m.survival_time for m in recent) / len(recent)
            avg_ep_efficiency = sum(m.ep_efficiency for m in recent) / len(recent)
            
            return {
                'games_analyzed': len(autonomous_ai.performance_history),
                'optimization_cycles': autonomous_ai.optimization_cycle,
                'recent_avg_kd_ratio': avg_kd,
                'recent_avg_survival_time': avg_survival,
                'recent_avg_ep_efficiency': avg_ep_efficiency,
                'current_strategy': await self.get_current_strategy(),
            }
            
        except Exception as e:
            log.error("🤖 Autonomous System: Performance summary failed: %s", e)
            return {"error": str(e)}

# Global autonomous bot manager
autonomous_manager = AutonomousBotManager()
