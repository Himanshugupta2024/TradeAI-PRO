import sqlite3
conn = sqlite3.connect(r'E:\trading\trading.db')
cur = conn.cursor()
cur.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")
for r in cur.fetchall():
    print(r[0])
    print(r[1])
    print()
conn.close()
