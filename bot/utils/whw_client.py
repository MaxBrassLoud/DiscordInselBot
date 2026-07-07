#!/usr/bin/env python3
"""
Velocity Whitelist & Maintenance API Client
Nutzt HMAC-SHA256 zur Authentifizierung.
"""

import argparse
import hashlib
import hmac
import json
import random
import string
import time
import uuid
from datetime import datetime

import requests

# ----------------------------------------------------------------------
# URL-Normalisierung
# ----------------------------------------------------------------------

def normalize_url(url):
    """Stellt sicher, dass die URL ein Protokoll und keinen trailing Slash hat."""
    url = url.strip()
    if not url.startswith(('http://', 'https://')):
        url = 'http://' + url
    return url.rstrip('/')  # trailing Slash entfernen, wird später wieder hinzugefügt

# ----------------------------------------------------------------------
# HMAC-Hilfsfunktionen
# ----------------------------------------------------------------------

def generate_nonce(length=16):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def generate_request_id():
    return str(uuid.uuid4())

def sign_request(secret, method, path, timestamp, nonce, request_id, body):
    message = f"{method}{path}{timestamp}{nonce}{request_id}{body}"
    return hmac.new(
        secret.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

def do_request(base_url, api_secret, method, path, data=None):
    """
    Führt einen authentifizierten Request aus.
    """
    url = base_url + path  # base_url endet ohne Slash, path beginnt mit /
    timestamp = str(int(time.time()))
    nonce = generate_nonce()
    request_id = generate_request_id()
    body = json.dumps(data) if data else ""

    signature = sign_request(api_secret, method, path, timestamp, nonce, request_id, body)

    headers = {
        'X-API-Key': 'client',
        'X-Request-ID': request_id,
        'X-Timestamp': timestamp,
        'X-Nonce': nonce,
        'X-Signature': signature,
        'Content-Type': 'application/json'
    }

    response = requests.request(method, url, headers=headers, data=body if data else None)
    return response

# ----------------------------------------------------------------------
# CLI – interaktive Befehle
# ----------------------------------------------------------------------

class ApiClient:
    def __init__(self, base_url, api_secret):
        self.base_url = base_url
        self.api_secret = api_secret
        self.running = True

    def cmd_whitelist_add(self, args):
        if len(args) < 1:
            print("Usage: whitelist add <player> [duration]")
            return
        player = args[0]
        duration = args[1] if len(args) > 1 else None
        data = {"player": player}
        if duration:
            data["duration"] = duration
        resp = do_request(self.base_url, self.api_secret, 'POST', '/api/v1/whitelist', data)
        if resp.status_code == 200:
            print(f"✅ Added {player} to whitelist.")
        else:
            print(f"❌ Error: {resp.status_code} - {resp.text}")

    def cmd_whitelist_remove(self, args):
        if len(args) < 1:
            print("Usage: whitelist remove <player>")
            return
        player = args[0]
        resp = do_request(self.base_url, self.api_secret, 'DELETE', f'/api/v1/whitelist/{player}')
        if resp.status_code == 200:
            print(f"✅ Removed {player} from whitelist.")
        else:
            print(f"❌ Error: {resp.status_code} - {resp.text}")

    def cmd_whitelist_list(self, args):
        resp = do_request(self.base_url, self.api_secret, 'GET', '/api/v1/whitelist')
        if resp.status_code == 200:
            entries = resp.json()
            if not entries:
                print("📋 Whitelist is empty.")
            else:
                print("📋 Whitelist entries:")
                for entry in entries:
                    expires = entry.get('expiresAt')
                    if expires:
                        expires_str = datetime.fromtimestamp(expires/1000).strftime('%Y-%m-%d %H:%M')
                    else:
                        expires_str = 'permanent'
                    print(f"  - {entry['name']} (added by {entry['addedBy']}, expires: {expires_str})")
        else:
            print(f"❌ Error: {resp.status_code} - {resp.text}")

    def cmd_maintenance_on(self, args):
        duration = args[0] if args else None
        data = {"enabled": True}
        if duration:
            data["duration"] = duration
        resp = do_request(self.base_url, self.api_secret, 'POST', '/api/v1/maintenance', data)
        if resp.status_code == 200:
            print(f"✅ Maintenance enabled." + (f" Duration: {duration}" if duration else ""))
        else:
            print(f"❌ Error: {resp.status_code} - {resp.text}")

    def cmd_maintenance_off(self, args):
        data = {"enabled": False}
        resp = do_request(self.base_url, self.api_secret, 'POST', '/api/v1/maintenance', data)
        if resp.status_code == 200:
            print("✅ Maintenance disabled.")
        else:
            print(f"❌ Error: {resp.status_code} - {resp.text}")

    def cmd_maintenance_add(self, args):
        print("ℹ️  Please use the server command /maintenance add <player> [duration]")
        print("   The API does not currently support adding individual players to maintenance bypass.")

    def cmd_maintenance_remove(self, args):
        print("ℹ️  Please use the server command /maintenance remove <player>")
        print("   The API does not currently support removing individual players from maintenance bypass.")

    def cmd_maintenance_status(self, args):
        resp = do_request(self.base_url, self.api_secret, 'GET', '/api/v1/maintenance')
        if resp.status_code == 200:
            data = resp.json()
            enabled = data.get('enabled', False)
            end_time = data.get('endTime')
            allowed = data.get('allowed', [])
            status_str = "ON" if enabled else "OFF"
            print(f"🛠️  Maintenance mode: {status_str}")
            if enabled and end_time:
                remaining = int((end_time - int(time.time()*1000)) / 1000)
                if remaining > 0:
                    print(f"   ⏳ Remaining: {remaining} seconds")
            if allowed:
                print("   Allowed players:")
                for p in allowed:
                    print(f"     - {p['name']} (expires: {p.get('expiresAt')})")
        else:
            print(f"❌ Error: {resp.status_code} - {resp.text}")

    def cmd_status(self, args):
        resp = do_request(self.base_url, self.api_secret, 'GET', '/api/v1/status')
        if resp.status_code == 200:
            data = resp.json()
            print("📊 Server Status:")
            print(f"  Mode: {data.get('mode')}")
            print(f"  Proxy: {data.get('proxy')}")
            print(f"  Whitelist count: {data.get('whitelistCount')}")
            print(f"  Maintenance: {'ON' if data.get('maintenance') else 'OFF'}")
            if data.get('maintenanceEnd'):
                print(f"  Maintenance ends: {datetime.fromtimestamp(data['maintenanceEnd']/1000).strftime('%Y-%m-%d %H:%M')}")
        else:
            print(f"❌ Error: {resp.status_code} - {resp.text}")

    def cmd_help(self, args):
        print("Available commands:")
        print("  whitelist add <player> [duration]")
        print("  whitelist remove <player>")
        print("  whitelist list")
        print("  maintenance on [duration]")
        print("  maintenance off")
        print("  maintenance add <player> [duration]  (not fully supported via API)")
        print("  maintenance remove <player>  (not fully supported via API)")
        print("  maintenance status")
        print("  status")
        print("  help")
        print("  exit")

    def run(self):
        print(f"🔐 Connected to API at {self.base_url}")
        print("Type 'help' for commands, 'exit' to quit.\n")
        while self.running:
            try:
                cmd_line = input("> ").strip()
                if not cmd_line:
                    continue
                parts = cmd_line.split()
                cmd = parts[0].lower()
                args = parts[1:]

                if cmd == "exit" or cmd == "quit":
                    self.running = False
                    break
                elif cmd == "help":
                    self.cmd_help(args)
                elif cmd == "whitelist":
                    if len(args) == 0:
                        print("Usage: whitelist add|remove|list")
                        continue
                    sub = args[0].lower()
                    sub_args = args[1:]
                    if sub == "add":
                        self.cmd_whitelist_add(sub_args)
                    elif sub == "remove":
                        self.cmd_whitelist_remove(sub_args)
                    elif sub == "list":
                        self.cmd_whitelist_list(sub_args)
                    else:
                        print(f"Unknown subcommand: {sub}")
                elif cmd == "maintenance":
                    if len(args) == 0:
                        print("Usage: maintenance on|off|add|remove|status")
                        continue
                    sub = args[0].lower()
                    sub_args = args[1:]
                    if sub == "on":
                        self.cmd_maintenance_on(sub_args)
                    elif sub == "off":
                        self.cmd_maintenance_off(sub_args)
                    elif sub == "add":
                        self.cmd_maintenance_add(sub_args)
                    elif sub == "remove":
                        self.cmd_maintenance_remove(sub_args)
                    elif sub == "status":
                        self.cmd_maintenance_status(sub_args)
                    else:
                        print(f"Unknown subcommand: {sub}")
                elif cmd == "status":
                    self.cmd_status(args)
                else:
                    print(f"Unknown command: {cmd}. Type 'help'.")
            except KeyboardInterrupt:
                print("\nExiting...")
                self.running = False
                break
            except Exception as e:
                print(f"❗ Error: {e}")

# ----------------------------------------------------------------------
# Hauptprogramm
# ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Velocity Whitelist & Maintenance API Client")
    parser.add_argument("--url", help="API base URL, e.g. http://localhost:8080")
    parser.add_argument("--secret", help="API secret key")
    args = parser.parse_args()

    if not args.url:
        args.url = input("Enter API URL (e.g. http://localhost:8080): ").strip()
    if not args.secret:
        args.secret = input("Enter API secret key: ").strip()

    # URL normalisieren (Protokoll ergänzen, trailing Slash entfernen)
    base_url = normalize_url(args.url)

    client = ApiClient(base_url, args.secret)
    client.run()

if __name__ == "__main__":
    main()