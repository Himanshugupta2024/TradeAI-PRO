"""Quick API test for Market tab endpoints."""
import urllib.request
import json

endpoints = [
    ("Gainers", "http://127.0.0.1:8000/api/screener/gainers"),
    ("Losers", "http://127.0.0.1:8000/api/screener/losers"),
    ("Sectors", "http://127.0.0.1:8000/api/sectors"),
    ("Health", "http://127.0.0.1:8000/api/health"),
]

for name, url in endpoints:
    try:
        r = urllib.request.urlopen(url, timeout=15)
        data = json.loads(r.read())
        print(f"\n=== {name} ({url}) ===")
        print(f"  Status: {r.status}")
        
        if name == "Gainers":
            items = data.get("gainers", [])
            print(f"  Count: {len(items)}")
            for x in items[:5]:
                sym = x.get("symbol", "?")
                chg = x.get("change_pct", 0)
                sec = x.get("sector", "")
                print(f"    {sym}: {chg:+.2f}% [{sec}]")
                
        elif name == "Losers":
            items = data.get("losers", [])
            print(f"  Count: {len(items)}")
            for x in items[:5]:
                sym = x.get("symbol", "?")
                chg = x.get("change_pct", 0)
                sec = x.get("sector", "")
                print(f"    {sym}: {chg:+.2f}% [{sec}]")
                
        elif name == "Sectors":
            items = data.get("sectors", [])
            print(f"  Count: {len(items)}")
            for x in items[:8]:
                sname = x.get("name", "?")
                chg = x.get("change", 0)
                icon = x.get("icon", "")
                print(f"    {icon} {sname}: {chg:+.2f}%")
                
        elif name == "Health":
            print(f"  Status: {data.get('status')}")
            print(f"  Symbols loaded: {data.get('symbols_loaded')}")
            print(f"  Signals: {data.get('signals_generated')}")
            
    except Exception as e:
        print(f"\n=== {name} ({url}) ===")
        print(f"  ERROR: {e}")

print("\n--- Test Complete ---")
