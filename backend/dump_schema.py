import sqlite3, os
p = os.environ.get('PRIVAGENT_DB_PATH', 'data/privagent.db')
print('db path:', p)
db = sqlite3.connect(p)
for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
    cols = [c[1] for c in db.execute(f'PRAGMA table_info({t})')]
    n = db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    print(t, 'rows=', n, 'cols=', cols)
