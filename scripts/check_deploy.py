import os
import urllib.request
import json
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv("RENDER_API_KEY")

if not api_key:
    print("No RENDER_API_KEY in environment or .env")
    exit(1)

req = urllib.request.Request(
    "https://api.render.com/v1/services/srv-d7bsr8juibrs739ujdj0/deploys?limit=3",
    headers={"Authorization": f"Bearer {api_key}"}
)

try:
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode())
        for item in data:
            d = item.get("deploy", {})
            print(f"Deploy ID: {d.get('id')} | Status: {d.get('status')} | Commit: {d.get('commit', {}).get('id', '')[:7]} | Msg: {d.get('commit', {}).get('message', '')[:50]}")
except Exception as e:
    print("Error fetching deploys:", e)
