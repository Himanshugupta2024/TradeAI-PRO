import sqlite3

conn = sqlite3.connect(r'E:\trading\trading.db')
cur = conn.cursor()

swing_symbols = [
    'TATASTEEL', 'ITC', 'JSWSTEEL', 'HINDALCO', 'SAIL', 'NATIONALUM',
    'NMDC', 'JINDALSAW', 'VEDL', 'COALINDIA', 'HAL', 'BEL', 'MAZDOCK',
    'COCHINSHIP', 'RAILVIKAS', 'IRFC', 'RITES', 'BHEL', 'TIINDIA',
    'GRINDWELL', 'ELECON', 'SIEMENS', 'ABB', 'POLYCAB', 'HAVELLS',
    'HGINFRA', 'CAPLIPOINT', 'SUNPHARMA', 'CIPLA', 'TRENT', 'DIXON',
    'IRCTC', 'KALYANKJIL', 'RELIANCE', 'SBIN', 'TATAMOTORS',
    'ADANIPORTS', 'TATAPOWER', 'GMDCLTD',
]

placeholders = ','.join(['?' for _ in swing_symbols])
cur.execute(
    "DELETE FROM ohlcv_cache WHERE timeframe='1d' AND symbol IN (" + placeholders + ")",
    swing_symbols
)
deleted = cur.rowcount
conn.commit()
conn.close()
print("Deleted", deleted, "stale daily rows. Restart server now.")
