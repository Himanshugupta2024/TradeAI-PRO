import os, urllib.request, json
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv("RENDER_API_KEY")
service_id = "srv-d7bsr8juibrs739ujdj0"
req = urllib.request.Request(
    f"https://api.render.com/v1/services/{service_id}/events?limit=10",
    headers={"Authorization": f"Bearer {api_key}"}
)
with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read().decode())
    for item in data:
        ev = item.get("event", {})
        print(ev.get("timestamp"), "|", ev.get("type"), "|", ev.get("details"))
