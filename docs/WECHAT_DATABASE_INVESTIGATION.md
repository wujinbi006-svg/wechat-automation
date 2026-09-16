# WeChat 4.x Database Investigation

> WeChat 4.1.13.12 | Windows 11 Home | Account: wxid_EXAMPLE
> Last Updated: 2026-09-02

## Account Structure

```
__USERPROFILE__\Documents\WeChat Files\
├── All Users
├── Applet
├── WMPF
├── wxid_EXAMPLE   ← CURRENT ACTIVE ACCOUNT
├── wxid_pmvuff944msb21    ← OLD ACCOUNT (2025)
└── wxid_EXAMPLE_FRIEND_A    ← OLD ACCOUNT (2025)
```

## Database Inventory (wxid_EXAMPLE)

### Message Databases (Multi/)

| File | Size | Encrypted | Purpose |
|------|------|:---------:|---------|
| MSG0.db | 62,914,560 | ✅ (header=00*16) | Messages (page 0) |
| MSG1.db | 62,914,560 | ✅ (header=00*16) | Messages (page 1) |
| MSG2.db | 62,914,560 | ✅ (header=00*16) | Messages (page 2) |
| MediaMSG0.db | 397,312 | ✅ | Media messages |
| MediaMSG1.db | 794,624 | ✅ | Media messages |
| MediaMSG2.db | 1,413,120 | ✅ | Media messages |
| FTSMSG0.db | 3,407,872 | ✅ | Full-text search index |
| FTSMSG1.db | 3,592,192 | ✅ | Full-text search index |
| FTSMSG2.db | 4,239,360 | ✅ | Full-text search index |

### Metadata Databases (Msg/)

| File | Size | Encrypted | Purpose |
|------|------|:---------:|---------|
| MicroMsg.db | 5,148,672 | ✅ | Account info, contacts |
| ChatMsg.db | 98,304 | ✅ | Chat metadata |
| ChatRoomUser.db | 421,888 | ✅ | Group chat members |
| Misc.db | 3,182,592 | ✅ | Miscellaneous |
| Sns.db | 3,731,456 | ✅ | Moments (朋友圈) |
| FTSContact.db | 946,176 | ✅ | Contact search index |
| Favorite.db | 311,296 | ✅ | Favorites |
| Emotion.db | 3,866,624 | ✅ | Emotion data |
| PublicMsg.db | 52,428,800 | ✅ | Public account messages |
| MultiSearchChatMsg.db | 1,441,792 | ✅ | Multi-search index |
| BizChat.db | 77,824 | ✅ | Business chat |
| BizChatMsg.db | 40,960 | ✅ | Business chat messages |
| OpenIMContact.db | 139,264 | ✅ | OpenIM contacts |
| OpenIMMsg.db | 167,936 | ✅ | OpenIM messages |
| Applet.db | 1,060,864 | ✅ | Mini programs |
| Voip.db | 20,480 | ✅ | VoIP logs |
| FunctionMsg.db | 90,112 | ✅ | Function messages |
| HardLinkFile.db | 45,056 | ✅ | File links |
| HardLinkImage.db | 86,016 | ✅ | Image links |
| HardLinkVideo.db | 28,672 | ✅ | Video links |
| xInfo.db | 32,768 | ❌ (plain SQLite) | Extended info |

### Config Files

```
__USERPROFILE__\Documents\WeChat Files\wxid_EXAMPLE\Config\
├── config.db (encrypted SQLCipher)
├── config.db-shm
├── config.db-wal
└── misc.ini
```

## Encryption Status

- **ALL databases are encrypted with SQLCipher**
- None of the database files have the standard SQLite header (`53 51 4C 69 74 65 20 66 6F 72 6D 61 74 20 33 00` = "SQLite format 3\0")
- Headers are randomized (different per file), typical of SQLCipher 4
- MSG0/1/2.db have all-zero first 16 bytes, which is consistent with SQLCipher 4 HMAC mode
- **xInfo.db is the only plain SQLite database** (standard header "SQLite format 3")

## Key File: xInfo.db

The only unencrypted database. Contains `53 51 4C 69 74 65 20 66 6F 72 6D 61 74 20 33 00` header.
Size: 32KB. Likely stores non-sensitive metadata or configuration.

## Database Access

- WeChat process holds exclusive locks on all database files
- WAL/SHM files are present with recent timestamps, confirming active use
- No separate SQLite module loaded in WeChat process (SQLCipher is statically linked)
- Database key is derived from device+account credentials, stored in memory

## Next Steps

1. **Read xInfo.db** - Determine its schema and contents (plain SQLite, no key needed)
2. **Study SQLCipher version** - Determine if SQLCipher 3 or 4 based on page size and header patterns
3. **Key research** - Investigate how WeChat derives the database key (password, hardware binding, etc.)
4. **DatabaseAdapter skeleton** - Create interface with read-only methods

## Notes

- All databases are actively used by WeChat (WAL files being written to)
- MSG0/1/2.db are sharded (approximately 60MB each, ~180MB total)
- File handles are held by WeChat process, so database files are locked
- This is a standard WeChat 4.x PC client database layout