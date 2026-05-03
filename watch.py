import os
import sys
import time
import subprocess
from pathlib import Path
from datetime import datetime

def get_mtime_dict(path: Path):
    """Scan directory for modification times of source files with intelligent filtering."""
    mtimes = {}
    # Folders to ignore
    ignore = {'.git', 'node_modules', '.venv', '__pycache__', '.gemini', '.antigravity', '.pytest_cache'}
    
    # Files to ignore (non-critical files that don't require restart)
    ignore_files = {
        'match_history.json', 'autonomous_ai_state.json', 'cascade_optimization_report.json',
        'logs.*.json', '.env.*', '*.tmp', '*.log', '*.bak'
    }
    
    # Critical files that ALWAYS require restart
    critical_files = {
        'brain.py', 'main.py', 'websocket_engine.py', 'api_client.py', 'autonomous_ai.py',
        'autonomous_integration.py', 'config.py', 'requirements.txt'
    }
    
    for p in path.glob('**/*'):
        # Skip ignored directories
        if any(part in ignore for part in p.parts):
            continue
            
        if p.is_file():
            file_name = p.name
            file_path = str(p)
            
            # Skip ignored file patterns
            if any(pattern.replace('*', '') in file_name for pattern in ignore_files):
                continue
                
            # Only watch specific file types
            if p.suffix in ('.py', '.env', '.json', '.md', '.txt'):
                try:
                    # Add priority flag for critical files
                    is_critical = file_name in critical_files
                    mtimes[file_path] = {
                        'mtime': p.stat().st_mtime,
                        'critical': is_critical,
                        'size': p.stat().st_size
                    }
                except FileNotFoundError:
                    continue
    return mtimes

def should_restart(changed_files: list, last_restart_time: float, cooldown_period: int = 3) -> bool:
    """Determine if restart is needed based on changed files and cooldown."""
    current_time = time.time()
    
    # Enforce cooldown period to prevent rapid restarts
    if current_time - last_restart_time < cooldown_period:
        return False, f"Cooldown active ({cooldown_period}s)"
    
    # Check if any critical files were changed
    critical_changes = [f for f in changed_files if f[2]]  # f[2] is the 'critical' flag
    
    if critical_changes:
        return True, f"Critical file(s) changed: {[Path(f[0]).name for f in critical_changes]}"
    
    # For non-critical files, only restart if multiple files changed
    if len(changed_files) >= 2:
        return True, f"Multiple files changed: {len(changed_files)}"
    
    # For single non-critical file changes, be more selective
    if len(changed_files) == 1:
        file_path, change_type, is_critical = changed_files[0]
        file_name = Path(file_path).name
        
        # Only restart for certain non-critical files
        restartable_files = {'strategy_dna.py', 'dashboard.py', 'state.py', 'action_sender.py'}
        if file_name in restartable_files:
            return True, f"Restartable file changed: {file_name}"
        else:
            return False, f"Non-critical file ignored: {file_name}"
    
    return False, "No significant changes"

def format_file_path(file_path: str) -> str:
    """Format file path for better display."""
    path = Path(file_path)
    # Show relative path from current directory
    try:
        rel_path = path.relative_to(Path.cwd())
        return str(rel_path)
    except ValueError:
        return path.name

def main():
    print("===========================================")
    print("   MOLTY ROYALE — SMART AUTO-RELOAD WATCHER")
    print("   Intelligent filtering to prevent stuck bot")
    print("   Critical files: brain.py, main.py, config.py")
    print("   Ignored files: match_history.json, logs, .tmp")
    print("===========================================")
    
    # Use the same python executable that's running this script
    cmd = [sys.executable, "-m", "bot.main"]
    
    current_mtimes = get_mtime_dict(Path('.'))
    critical_count = sum(1 for v in current_mtimes.values() if v['critical'])
    
    # Start the bot process
    print(f"[Watcher] 🚀 Starting bot: {' '.join(cmd)}")
    print(f"[Watcher] 📁 Watching {len(current_mtimes)} files ({critical_count} critical)")
    process = subprocess.Popen(cmd)
    
    restart_count = 0
    last_restart_time = 0
    ignored_changes = 0
    
    try:
        while True:
            time.sleep(1.0)  # Slightly slower polling to reduce CPU usage
            
            # Check for changes
            new_mtimes = get_mtime_dict(Path('.'))
            
            changed_files = []
            current_time = time.time()
            
            # Find changed files with new data structure
            for file_path, new_data in new_mtimes.items():
                if file_path not in current_mtimes:
                    # New file created
                    changed_files.append((file_path, "created", new_data['critical']))
                elif new_data['mtime'] > current_mtimes[file_path]['mtime']:
                    # File modified - check for significant change
                    time_diff = new_data['mtime'] - current_mtimes[file_path]['mtime']
                    size_diff = abs(new_data['size'] - current_mtimes[file_path]['size'])
                    
                    # Only consider it a change if timestamp difference > 0.5s OR size changed
                    if time_diff > 0.5 or size_diff > 0:
                        changed_files.append((file_path, "modified", new_data['critical']))
            
            # Check for deleted files
            for file_path in current_mtimes:
                if file_path not in new_mtimes:
                    changed_files.append((file_path, "deleted", current_mtimes[file_path]['critical']))
            
            # Process changes if any detected
            if changed_files:
                should_restart_now, reason = should_restart(changed_files, last_restart_time, cooldown_period=3)
                
                if should_restart_now:
                    last_restart_time = current_time
                    restart_count += 1
                    
                    print(f"\n[Watcher] 🔄 RESTART REQUIRED! (#{restart_count})")
                    print(f"[Watcher] 📝 Reason: {reason}")
                    print("[Watcher] 📝 Changed files:")
                    for file_path, change_type, is_critical in changed_files:
                        formatted_path = format_file_path(file_path)
                        critical_marker = " 🔴" if is_critical else " 🟡"
                        print(f"          • {change_type.upper():8} | {formatted_path}{critical_marker}")
                    
                    # Kill existing process gracefully
                    print("[Watcher] ⏹️  Stopping current bot process...")
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                        print("[Watcher] ✅ Bot stopped gracefully")
                    except subprocess.TimeoutExpired:
                        # Force kill if it doesn't stop
                        print("[Watcher] ⚡ Force killing bot process...")
                        subprocess.call(['taskkill', '/F', '/T', '/PID', str(process.pid)], 
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    
                    # Cooldown for port to free up
                    time.sleep(1)
                    
                    # Restart
                    print(f"[Watcher] 🚀 Restarting bot...")
                    process = subprocess.Popen(cmd)
                    current_mtimes = new_mtimes
                    print(f"[Watcher] ✅ Bot restarted successfully!")
                    
                else:
                    # Log ignored changes for debugging
                    ignored_changes += 1
                    if ignored_changes % 5 == 1:  # Show every 5th ignored change to avoid spam
                        print(f"[Watcher] ⏸️  Changes ignored: {reason}")
                        print(f"[Watcher] 📊 Total ignored: {ignored_changes} (to prevent stuck bot)")
                
            # If process died unexpectedly, restart it
            if process.poll() is not None:
                print(f"[Watcher] ⚠️  Bot stopped (Exit Code: {process.returncode}). Restarting in 2s...")
                time.sleep(2)
                process = subprocess.Popen(cmd)
                current_mtimes = get_mtime_dict(Path('.'))
                last_restart_time = time.time()
                
    except KeyboardInterrupt:
        print("\n[Watcher] 🛑 Stopping watcher...")
        print("[Watcher] ⏹️  Stopping bot process...")
        process.terminate()
        try:
            process.wait(timeout=2)
            print("[Watcher] ✅ Clean shutdown completed")
        except:
            print("[Watcher] ⚡ Force killing bot process...")
            subprocess.call(['taskkill', '/F', '/T', '/PID', str(process.pid)], 
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[Watcher] 📊 Session summary: {restart_count} restarts, {ignored_changes} changes ignored")
        print("[Watcher] 👋 Goodbye!")
        sys.exit(0)

if __name__ == "__main__":
    main()
