import glob, os, sqlite3, json

root = r"__ROOT__\work\decrypted_db"
target = "example_alias_a"

files = sorted(glob.glob(os.path.join(root, "*_contact.db")),
               key=os.path.getmtime, reverse=True)
print("contact.db cache files:", len(files))

seen = set()
for p in files[:12]:
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tabs = [r[0] for r in cur.fetchall()]
        for t in tabs:
            if "contact" not in t.lower() and t.lower() not in ("contact",):
                continue
            cur.execute(f'PRAGMA table_info("{t}")')
            cols = [r[1] for r in cur.fetchall()]
            if not cols:
                continue
            where = " OR ".join([f'"{c}" LIKE ?' for c in cols])
            params = [f"%{target}%"] * len(cols)
            try:
                cur.execute(f'SELECT * FROM "{t}" WHERE {where} LIMIT 5', params)
                rows = cur.fetchall()
            except Exception as e:
                rows = []
            if rows:
                print("=" * 70)
                print("FILE:", os.path.basename(p))
                print("TABLE:", t)
                print("COLS:", cols)
                for r in rows:
                    print("  ROW:", dict(zip(cols, r)))
        con.close()
    except Exception as e:
        print("ERR", os.path.basename(p), e)
