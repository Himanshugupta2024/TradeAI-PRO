import os
import json
import urllib.request
from dotenv import load_dotenv

load_dotenv()
RENDER_API_KEY = os.getenv("RENDER_API_KEY")
SERVICE_ID = "srv-d7bsr8juibrs739ujdj0"

req = urllib.request.Request(
    f"https://api.render.com/v1/services/{SERVICE_ID}/deploys",
    data=json.dumps({"clearCache": "clear"}).encode(),
    headers={
        "Authorization": f"Bearer {RENDER_API_KEY}",
        "Content-Type": "application/json"
    },
    method="POST"
)

try:
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode())
        print(f"Deploy successfully triggered! ID: {data.get('id')} | Status: {data.get('status')}")
except Exception as e:
    print("Error triggering deploy:", e)
    if hasattr(e, "read"):
        print("Details:", e.read().decode())
