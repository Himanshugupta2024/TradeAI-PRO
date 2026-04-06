"""Quick script to check signal distribution from the running server."""
import urllib.request
import json

try:
    url = "http://127.0.0.1:8000/api/signals/all"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
    
    signals = data.get("signals", [])
    print(f"Total signals: {len(signals)}")
    
    # Count by type
    counts = {}
    for s in signals:
        sig = s.get("signal", "UNKNOWN")
        counts[sig] = counts.get(sig, 0) + 1
    
    for sig_type, count in sorted(counts.items()):
        print(f"  {sig_type}: {count}")
    
    print(f"\nPer-symbol breakdown:")
    for s in signals:
        symbol = s.get("symbol", "?")
        signal = s.get("signal", "?")
        conf = s.get("confidence", 0)
        trend = s.get("trend", "?")
        rsi = s.get("rsi", 0)
        print(f"  {symbol:15s} | {signal:5s} | conf={conf:5.1f} | trend={trend:10s} | RSI={rsi:.1f}")

except Exception as e:
    print(f"Error: {e}")
