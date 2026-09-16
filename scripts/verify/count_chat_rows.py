import glob, hashlib, os, sqlite3

chat = "wxid_EXAMPLE_FRIEND_A"
table = "Msg_" + hashlib.md5(chat.encode("utf-8")).hexdigest()
print("target table:", table)

root = r"__ROOT__\work\decrypted_db"
found = []
for p in glob.glob(os.path.join(root, "*.db")):
    try:
        if os.path.getsize(p) < 8192:
            continue
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        row = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row:
            cur = con.execute(f'SELECT COUNT(*), MIN(sort_seq), MAX(sort_seq) FROM "{table}"')
            n, mn, mx = cur.fetchone()
            found.append((os.path.basename(p), os.path.getsize(p), n, mn, mx))
        con.close()
    except Exception:
        pass

print("hit files:", len(found))
for f in found:
    print(" ", f)
