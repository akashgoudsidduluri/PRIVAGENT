import sqlite3, shutil, os
p = 'data/privagent.db'
shutil.copy(p, 'data/privagent.db.bak-2026-11-19')
for suffix in ('-wal', '-shm'):
    src = p + suffix
    if os.path.exists(src):
        shutil.copy(src, src + '.bak-2026-11-19')
db = sqlite3.connect(p)
cols = [c[1] for c in db.execute('PRAGMA table_info(sessions)')]
print('sessions cols before:', cols)
if 'user_id' not in cols:
    db.execute('ALTER TABLE sessions RENAME TO sessions_legacy_extension')
    db.commit()
    print('renamed old sessions -> sessions_legacy_extension, rows kept')
else:
    print('user_id already present, nothing to rename')
db.close()
print('backup: data/privagent.db.bak-2026-11-19')
