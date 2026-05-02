"""
Cascade AI Bridge - Integration with Agentic AI Chat System
Allows autonomous AI to report optimizations and receive guidance
"""
import json
import time
from typing import Dict, Any
from bot.utils.logger import get_logger

log = get_logger(__name__)

class CascadeAIBridge:
    """Bridge between autonomous AI and Cascade chat system"""
    
    def __init__(self):
        self.last_report_time = 0
        self.report_interval = 300  # 5 minutes between reports
        
    async def check_optimization_reports(self) -> Dict[str, Any]:
        """Check for new optimization reports from autonomous AI"""
        try:
            # Read latest optimization report
            with open('d:\\AI game\\moltyRyle1.6.0\\cascade_optimization_report.json', 'r') as f:
                report = json.load(f)
            
            log.info("🤖 Cascade Bridge: Optimization report found - Cycle %d", 
                    report.get('cycle', 0))
            
            return report
            
        except FileNotFoundError:
            return {"status": "no_report", "message": "No optimization report available"}
        except Exception as e:
            log.error("🤖 Cascade Bridge: Failed to read report: %s", e)
            return {"status": "error", "message": str(e)}
    
    async def analyze_optimization_trends(self) -> Dict[str, Any]:
        """Analyze optimization trends for Cascade AI review"""
        try:
            # Read optimization history
            with open('d:\\AI game\\moltyRyle1.6.0\\autonomous_ai_state.json', 'r') as f:
                state = json.load(f)
            
            performance_history = state.get('performance_history', [])
            current_params = state.get('strategy_params', {})
            optimization_cycle = state.get('optimization_cycle', 0)
            
            # Analyze trends
            if len(performance_history) >= 3:
                recent_games = performance_history[-3:]
                avg_kd = sum(game['kd_ratio'] for game in recent_games) / len(recent_games)
                avg_survival = sum(game['survival_time'] for game in recent_games) / len(recent_games)
                avg_ep_efficiency = sum(game['ep_efficiency'] for game in recent_games) / len(recent_games)
                
                trend_analysis = {
                    'performance_trend': 'improving' if avg_kd > 0.5 else 'declining',
                    'aggression_level': current_params.get('aggression_level', 0.7),
                    'weak_enemy_threshold': current_params.get('weak_enemy_threshold', 60),
                    'ep_conservation_threshold': current_params.get('ep_conservation_threshold', 8),
                    'optimization_cycle': optimization_cycle,
                    'recent_avg_kd': avg_kd,
                    'recent_avg_survival': avg_survival,
                    'recent_avg_ep_efficiency': avg_ep_efficiency,
                    'games_analyzed': len(performance_history),
                }
                
                # Generate recommendations for Cascade AI
                recommendations = []
                
                if avg_kd < 0.3:
                    recommendations.append("Consider reducing aggression - K/D ratio too low")
                elif avg_kd > 1.0:
                    recommendations.append("Aggression working well - maintain or increase slightly")
                
                if avg_ep_efficiency < 0.4:
                    recommendations.append("EP management needs improvement - increase conservation")
                elif avg_ep_efficiency > 0.8:
                    recommendations.append("EP efficiency excellent - can be more aggressive")
                
                if avg_survival < 120:  # Less than 2 minutes
                    recommendations.append("Survival time low - prioritize escape over combat")
                
                trend_analysis['recommendations'] = recommendations
                
                log.info("🤖 Cascade Bridge: Trend analysis complete - %d games analyzed", 
                        len(performance_history))
                
                return trend_analysis
            
            else:
                return {
                    "status": "insufficient_data",
                    "message": f"Need at least 3 games, have {len(performance_history)}",
                    "games_analyzed": len(performance_history)
                }
                
        except FileNotFoundError:
            return {"status": "no_state", "message": "No autonomous AI state found"}
        except Exception as e:
            log.error("🤖 Cascade Bridge: Analysis failed: %s", e)
            return {"status": "error", "message": str(e)}
    
    async def generate_cascade_summary(self) -> str:
        """Generate summary for Cascade AI chat review"""
        try:
            report = await self.check_optimization_reports()
            trends = await self.analyze_optimization_trends()
            
            summary = f"""
🤖 **Autonomous AI Optimization Summary**

**Current Status:**
- Optimization Cycle: {trends.get('optimization_cycle', 'N/A')}
- Games Analyzed: {trends.get('games_analyzed', 0)}
- Performance Trend: {trends.get('performance_trend', 'unknown')}

**Strategy Parameters:**
- Aggression Level: {trends.get('aggression_level', 'N/A')}
- Weak Enemy Threshold: {trends.get('weak_enemy_threshold', 'N/A')}
- EP Conservation: {trends.get('ep_conservation_threshold', 'N/A')}

**Recent Performance:**
- Avg K/D Ratio: {trends.get('recent_avg_kd', 'N/A')}
- Avg Survival Time: {trends.get('recent_avg_survival_time', 'N/A')}s
- Avg EP Efficiency: {trends.get('recent_avg_ep_efficiency', 'N/A')}

**AI Recommendations:**
{chr(10).join(f"- {rec}" for rec in trends.get('recommendations', ['No recommendations yet']))}

**Next Steps:**
- Continue monitoring performance
- Autonomous AI will auto-adjust parameters
- Review trends after 5+ games for better insights
"""
            
            return summary
            
        except Exception as e:
            log.error("🤖 Cascade Bridge: Summary generation failed: %s", e)
            return f"Error generating summary: {str(e)}"

# Global bridge instance
cascade_bridge = CascadeAIBridge()
