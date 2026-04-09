import requests

for sym in ['TATASTEEL', 'ITC', 'CUPID', 'PRECWIRE']:
    try:
        r = requests.get(f'http://localhost:8000/api/swing-debug?symbol={sym}', timeout=20)
        d = r.json()
        print(f'\n=== {sym} ===')
        print(f'  in_daily_cache : {d.get("in_daily_cache")}')
        print(f'  bars           : {d.get("bars")}')
        print(f'  raw patterns   : {d.get("raw_setups_count")}')
        print(f'  after gates    : {d.get("after_gates_count")}')
        print(f'  drop_reasons   : {d.get("drop_reasons")}')
        print(f'  ltp            : {d.get("ltp")}')
        print(f'  error          : {d.get("error")}')
    except Exception as e:
        print(f'{sym}: REQUEST FAILED - {e}')
