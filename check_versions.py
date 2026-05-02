#!/usr/bin/env python3
"""Version check script for debugging environment differences"""
import sys
import websockets
import aiohttp

print("=== Environment Version Check ===")
print(f"Python version: {sys.version}")
print(f"Websockets version: {websockets.__version__}")
print(f"aiohttp version: {aiohttp.__version__}")

# Check websockets API compatibility
print("\n=== Websockets API Check ===")
try:
    # Test if extra_headers is supported
    import inspect
    connect_sig = inspect.signature(websockets.connect)
    params = list(connect_sig.parameters.keys())
    print(f"websockets.connect parameters: {params}")
    
    has_extra_headers = 'extra_headers' in params
    has_additional_headers = 'additional_headers' in params
    print(f"Supports extra_headers: {has_extra_headers}")
    print(f"Supports additional_headers: {has_additional_headers}")
    
except Exception as e:
    print(f"API check failed: {e}")

print("\n=== Platform Info ===")
print(f"Platform: {sys.platform}")
print(f"Architecture: {sys.architecture if hasattr(sys, 'architecture') else 'Unknown'}")
