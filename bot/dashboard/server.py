"""
Dashboard web server — serves the UI and real-time WebSocket updates.
Uses aiohttp for lightweight async HTTP + WebSocket.
"""
import os
import json
import asyncio
from aiohttp import web
from bot.dashboard.state import dashboard_state
from bot.learning.strategy_dna import DEFAULT_DNA, StrategyDNA, DNA_FILE, MATCH_HISTORY_FILE, sanitize_dna
from bot.utils.logger import get_logger

log = get_logger(__name__)

DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(DASHBOARD_DIR, "static")

# Connected WebSocket clients for real-time push
_ws_clients: set = set()


async def index_handler(request):
    """Serve the dashboard HTML (no cache to always get latest)."""
    html_path = os.path.join(STATIC_DIR, "index.html")
    resp = web.FileResponse(html_path)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


async def api_state(request):
    """Return full dashboard state snapshot."""
    return web.json_response(dashboard_state.get_snapshot())


async def api_accounts(request):
    """Return accounts list."""
    return web.json_response({"accounts": dashboard_state.accounts})


async def api_export(request):
    """Export all data as JSON download."""
    data = dashboard_state.get_snapshot()
    return web.json_response(data, headers={
        "Content-Disposition": "attachment; filename=molty-export.json"
    })


def _load_json_file(path: str, fallback):
    """Load JSON data for dashboard widgets."""
    try:
        if not os.path.exists(path):
            return fallback
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return fallback


def _num(value, default=0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _fitness(match: dict) -> float:
    return StrategyDNA().calculate_fitness({
        "placement": _num(match.get("placement", 100), 100),
        "kills": _num(match.get("kills", 0), 0),
        "survival_time": _num(match.get("survival_time", 0), 0),
        "damage_dealt": _num(match.get("damage_dealt", 0), 0),
    })


async def api_learning(request):
    """Return learning DNA + match history summary for the web dashboard."""
    history = _load_json_file(MATCH_HISTORY_FILE, [])
    if not isinstance(history, list):
        history = []

    raw_dna = _load_json_file(DNA_FILE, {})
    dna_saved = bool(raw_dna)
    dna = sanitize_dna(raw_dna or DEFAULT_DNA)

    placements = [_num(m.get("placement", 100), 100) for m in history]
    kills = [_num(m.get("kills", 0), 0) for m in history]
    survival = [_num(m.get("survival_time", 0), 0) for m in history]
    damage = [_num(m.get("damage_dealt", 0), 0) for m in history]

    total = len(history)
    wins = sum(1 for p in placements if int(p) == 1)
    top10 = sum(1 for p in placements if p <= 10)

    recent = []
    for match in list(reversed(history[-10:])):
        fitness = match.get("fitness")
        if fitness is None:
            fitness = _fitness(match)
        recent.append({
            "timestamp": str(match.get("timestamp", ""))[:16],
            "placement": int(_num(match.get("placement", 100), 100)),
            "kills": int(_num(match.get("kills", 0), 0)),
            "survival_time": int(_num(match.get("survival_time", 0), 0)),
            "damage_dealt": int(_num(match.get("damage_dealt", 0), 0)),
            "fitness": round(_num(fitness, 0), 1),
        })

    avg_kills = (sum(kills) / total) if total else 0
    avg_placement = (sum(placements) / total) if total else 0
    recommendations = []
    if total < 5:
        recommendations.append("Data masih sedikit. Kumpulkan minimal 5 match supaya evolusi DNA lebih masuk akal.")
    if total and avg_placement > 50:
        recommendations.append("Survival masih rendah. Pertahankan combat HP floor dan jangan terlalu cepat cari fight.")
    if total and avg_kills < 1:
        recommendations.append("Kill masih rendah. Naikkan agresi late game hanya kalau survival sudah membaik.")
    if total and avg_placement <= 10:
        recommendations.append("Placement bagus. Jaga balance survival dan combat seperti sekarang.")
    if not recommendations:
        recommendations.append("Learning siap. Lanjut kumpulkan match untuk melihat tren yang lebih jelas.")

    return web.json_response({
        "dna": dna,
        "dna_saved": dna_saved,
        "summary": {
            "total_matches": total,
            "wins": wins,
            "top10": top10,
            "avg_placement": round((sum(placements) / total) if total else 0, 1),
            "avg_kills": round(avg_kills, 1),
            "avg_survival": round((sum(survival) / total) if total else 0),
            "total_kills": int(sum(kills)),
            "total_damage": int(sum(damage)),
        },
        "recent": recent,
        "recommendations": recommendations,
    })


async def ws_handler(request):
    """WebSocket endpoint — client stays connected for push updates."""
    ws = web.WebSocketResponse(heartbeat=30)  # 30s ping/pong keepalive
    await ws.prepare(request)
    _ws_clients.add(ws)
    log.info("Dashboard WS client connected (%d total)", len(_ws_clients))

    try:
        # Send initial snapshot
        snapshot = dashboard_state.get_snapshot()
        await ws.send_json({"type": "snapshot", "data": snapshot})

        # Keep connection alive — listen for client messages
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                pass  # No client commands yet
            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    except Exception as e:
        log.debug("WS handler error: %s", e)
    finally:
        _ws_clients.discard(ws)
        log.info("Dashboard WS client disconnected (%d remaining)", len(_ws_clients))

    return ws


async def _push_loop(app):
    """Background task: push state snapshots to all WS clients every 1.5s."""
    global _ws_clients
    log.info("Dashboard push loop started")
    try:
        while True:
            await asyncio.sleep(1.5)
            if not _ws_clients:
                continue
            try:
                snapshot = dashboard_state.get_snapshot()
                msg = json.dumps({"type": "snapshot", "data": snapshot})
                dead = set()
                for ws in list(_ws_clients):  # Copy set to avoid mutation during iteration
                    try:
                        await ws.send_str(msg)
                    except Exception:
                        dead.add(ws)
                if dead:
                    _ws_clients -= dead
                    log.debug("Removed %d dead WS clients", len(dead))
            except Exception as e:
                log.warning("Dashboard push error: %s", e)
    except asyncio.CancelledError:
        log.info("Dashboard push loop stopped")


async def start_push_loop(app):
    """Start push loop as background task on app startup."""
    app['push_task'] = asyncio.create_task(_push_loop(app))


async def stop_push_loop(app):
    """Stop push loop on app shutdown."""
    task = app.get('push_task')
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def api_accounts_post(request):
    """Save account from dashboard form."""
    try:
        data = await request.json()
        dashboard_state.set_account(data)
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)


async def api_import(request):
    """Import data from JSON."""
    try:
        data = await request.json()
        if "accounts" in data:
            for acc in data["accounts"]:
                dashboard_state.set_account(acc)
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)


def create_app() -> web.Application:
    """Create the aiohttp web application."""
    app = web.Application()

    # Routes
    app.router.add_get("/", index_handler)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/accounts", api_accounts)
    app.router.add_post("/api/accounts", api_accounts_post)
    app.router.add_get("/api/learning", api_learning)
    app.router.add_get("/api/export", api_export)
    app.router.add_post("/api/import", api_import)
    app.router.add_get("/ws", ws_handler)

    # Static files
    if os.path.exists(STATIC_DIR):
        app.router.add_static("/static/", STATIC_DIR)

    # Background push loop — uses aiohttp lifecycle hooks (reliable)
    app.on_startup.append(start_push_loop)
    app.on_cleanup.append(stop_push_loop)

    return app


async def start_dashboard(port: int = 8080):
    """Start the dashboard server (non-blocking)."""
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("===========================================")
    log.info("  Dashboard running at http://0.0.0.0:%d", port)
    log.info("===========================================")
