"""
Molty Royale AI Agent — Entry Point v2.0.
Run: python -m bot.main
Dashboard + Bot run concurrently.
"""
import asyncio
import os
import sys
from bot.heartbeat import Heartbeat
from bot.dashboard.server import start_dashboard
from bot.utils.logger import get_logger
from bot.autonomous_integration import autonomous_manager

log = get_logger(__name__)

# Railway injects PORT env var; fallback to DASHBOARD_PORT or 8080
DASHBOARD_PORT = int(os.getenv("PORT", os.getenv("DASHBOARD_PORT", "8080")))


async def main():
    """Entry point for the bot with autonomous AI integration."""
    log.info("Molty Royale AI Agent v1.6.0")
    log.info("By Eryck Juliant")
    log.info("🤖 Autonomous AI System: Initializing...")
    
    # Initialize autonomous AI system
    await autonomous_manager.initialize_autonomous_system()
    
    log.info("Press Ctrl+C to stop")

    heartbeat = Heartbeat()

    async def run_all():
        # Start dashboard server (non-blocking)
        await start_dashboard(port=DASHBOARD_PORT)
        # Run heartbeat (main bot loop — runs forever)
        await heartbeat.run()

    try:
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        await run_all()
    except KeyboardInterrupt:
        log.info("Shutdown complete.")

def main_sync():
    """Synchronous entry point for backwards compatibility."""
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
