import os
import sys
import time
import subprocess
from pathlib import Path

def get_mtime_dict(path: Path):
    """Scan directory for modification times of source files."""
    mtimes = {}
    # Folders to ignore
    ignore = {'.git', 'node_modules', '.venv', '__pycache__', '.gemini', '.antigravity'}
    
    for p in path.glob('**/*'):
        # Skip ignored directories
        if any(part in ignore for part in p.parts):
            continue
            
        if p.suffix in ('.py', '.env', '.json') and p.is_file():
            try:
                mtimes[str(p)] = p.stat().st_mtime
            except FileNotFoundError:
                continue
    return mtimes

def main():
    print("===========================================")
    print("   MOLTY ROYALE — AUTO-RELOAD WATCHER")
    print("   Watching: .py, .env, .json")
    print("===========================================")
    
    # Use the same python executable that's running this script
    cmd = [sys.executable, "-m", "bot.main"]
    
    current_mtimes = get_mtime_dict(Path('.'))
    
    # Start the bot process
    print(f"[Watcher] Starting bot: {' '.join(cmd)}")
    process = subprocess.Popen(cmd)
    
    try:
        while True:
            time.sleep(1.5)  # Poll every 1.5 seconds
            
            # Check for changes
            new_mtimes = get_mtime_dict(Path('.'))
            
            changed = False
            if len(new_mtimes) != len(current_mtimes):
                changed = True
            else:
                for k, v in new_mtimes.items():
                    if k not in current_mtimes or v > current_mtimes[k]:
                        # Only trigger if mtime difference is significant (> 0.1s)
                        if k not in current_mtimes or abs(v - current_mtimes[k]) > 0.1:
                            changed = True
                            print(f"[Watcher] Change detected in: {Path(k).name}")
                            break
            
            if changed:
                print(f"[Watcher] Restarting bot...")
                # Kill existing process
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # Force kill if it doesn't stop
                    subprocess.call(['taskkill', '/F', '/T', '/PID', str(process.pid)], 
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
                # Small cooldown for port to free up
                time.sleep(1)
                
                # Restart
                process = subprocess.Popen(cmd)
                current_mtimes = new_mtimes
                
            # If process died unexpectedly, restart it (unless it was killed by us)
            if process.poll() is not None:
                print(f"[Watcher] Bot stopped (Exit Code: {process.returncode}). Restarting in 3s...")
                time.sleep(3)
                process = subprocess.Popen(cmd)
                current_mtimes = get_mtime_dict(Path('.'))
                
    except KeyboardInterrupt:
        print("\n[Watcher] Stopping...")
        process.terminate()
        try:
            process.wait(timeout=2)
        except:
            subprocess.call(['taskkill', '/F', '/T', '/PID', str(process.pid)], 
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        sys.exit(0)

if __name__ == "__main__":
    main()
