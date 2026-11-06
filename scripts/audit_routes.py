import re

with open('api/routes.py', 'r', encoding='utf-8') as f:
    text = f.read()

private_keywords = ['portfolio', 'paper', 'capital', 'journal', 'trade', 'risk', 'orders', 'positions']
matches = re.findall(r'@(?:router|app)\.(get|post|put|delete)\(["\']([^"\']+)["\']', text)

print("=== SENSITIVE / PRIVATE GET ROUTES ===")
for method, path in matches:
    if method == 'get' and any(k in path for k in private_keywords):
        print(f"GET  {path}")

print("\n=== PUBLIC MARKET DATA GET ROUTES ===")
for method, path in matches:
    if method == 'get' and not any(k in path for k in private_keywords):
        if any(p in path for p in ['overview', 'signals', 'market', 'screener', 'swing', 'breakouts', 'health', 'ready']):
            print(f"GET  {path}")
