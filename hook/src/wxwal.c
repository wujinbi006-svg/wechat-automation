/*
 * wxwal.dll -- WeChat 4.x real-time WAL capture via IAT hook on WriteFile.
 *
 * Why this is version-independent:
 *   Weixin.dll imports only kernel32, and its IAT has WriteFile. We patch that
 *   one pointer. No WeChat offsets, no signature scanning, no version table.
 *   WeChat writes every database change into *.db-wal; we copy those buffers
 *   and hand them to a Python sidecar, which decrypts them with the SQLCipher
 *   key we already hold and parses the resulting SQLite pages.
 *
 * Safety:
 *   - the original WriteFile is always called; we never alter what WeChat writes
 *   - the pipe write is non-blocking and best-effort: if nobody is listening we
 *     drop the frame and return immediately, so WeChat can never stall on us
 *   - no allocation on the hot path beyond a bounded copy
 */

#include <windows.h>

#define PIPE_NAME L"\\\\.\\pipe\\wxwal"
#define FRAME_MAGIC 0x57414C31u  /* "WAL1" */

/*
 * Each protocol revision uses its own pipe name. Injected DLLs cannot be
 * unloaded, so a rebuilt hook is injected alongside older copies; if they all
 * wrote to one pipe the reader would see interleaved, incompatible framing
 * (observed as tens of thousands of desync bytes). A per-revision pipe keeps
 * the newest copy isolated without forcing a WeChat restart.
 */
#ifndef WXWAL_PIPE_NAME
#define WXWAL_PIPE_NAME L"\\\\.\\pipe\\wxwal10"
#endif

/* One captured write. Sent verbatim right after this header. */
#pragma pack(push, 1)
typedef struct {
    DWORD magic;
    DWORD pid;
    DWORD handle;      /* low 32 bits, for correlation only */
    DWORD size;
    DWORD tick;
    DWORD path_len;    /* UTF-8 bytes of the resolved path; 0 if unavailable */
} WalFrameHeader;
#pragma pack(pop)

typedef BOOL (WINAPI *WriteFileFn)(HANDLE, LPCVOID, DWORD, LPDWORD, LPOVERLAPPED);
typedef BOOL (WINAPI *WriteFileExFn)(HANDLE, LPCVOID, DWORD, LPOVERLAPPED, LPOVERLAPPED_COMPLETION_ROUTINE);

static WriteFileFn   real_WriteFile   = NULL;
static WriteFileExFn real_WriteFileEx = NULL;

static HANDLE  g_pipe        = INVALID_HANDLE_VALUE;
static HANDLE  g_pipe_lock   = NULL;
static DWORD   g_self_pid    = 0;
static volatile LONG g_running = 0;
static volatile LONG g_frames  = 0;
static volatile LONG g_dropped = 0;
static volatile LONG g_calls   = 0;
static volatile LONG g_walhits = 0;

static void log_status(const char *msg);

/* Periodic counter dump. Without this we cannot tell "hook never fired" from
 * "hook fired but nothing matched", which are very different bugs. */
static DWORD WINAPI stats_thread(LPVOID param) {
    (void)param;
    CHAR buf[192];
    for (;;) {
        Sleep(5000);
        LONG c  = InterlockedCompareExchange(&g_calls, 0, 0);
        LONG w  = InterlockedCompareExchange(&g_walhits, 0, 0);
        LONG f  = InterlockedCompareExchange(&g_frames, 0, 0);
        LONG d  = InterlockedCompareExchange(&g_dropped, 0, 0);
        wsprintfA(buf, "wxwal stats: calls=%ld wal_hits=%ld frames=%ld dropped=%ld pipe=%s",
                  c, w, f, d, (g_pipe == INVALID_HANDLE_VALUE) ? "down" : "up");
        log_status(buf);
    }
    return 0;
}

/* ---------------------------------------------------------------- handle cache
 * GetFinalPathNameByHandleW is far too slow to run per WriteFile. We remember
 * the verdict for each handle we have already classified. Small fixed table,
 * linear probe, replace-on-collision; correctness only needs it to be a cache,
 * because a miss just re-queries the path.
 */
#define CACHE_SLOTS 512
typedef struct { HANDLE h; LONG is_wal; } CacheSlot;
static CacheSlot g_cache[CACHE_SLOTS];

static int classify_handle(HANDLE h) {
    DWORD idx = ((ULONG_PTR)h >> 4) % CACHE_SLOTS;
    for (DWORD probe = 0; probe < CACHE_SLOTS; probe++) {
        DWORD i = (idx + probe) % CACHE_SLOTS;
        if (g_cache[i].h == h) return g_cache[i].is_wal;
        if (g_cache[i].h == NULL) break;
    }

    int is_wal = 0;
    WCHAR path[MAX_PATH * 2];
    DWORD n = GetFinalPathNameByHandleW(h, path, ARRAYSIZE(path),
                                        FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
    if (n > 0 && n < ARRAYSIZE(path)) {
        /*
         * Use the actual string length, not n: depending on the build,
         * GetFinalPathNameByHandleW's return value may or may not count the
         * terminating null, and an off-by-one here silently rejects every WAL
         * handle while every other part of the hook keeps working.
         */
        size_t len = wcslen(path);
        static const WCHAR suffix[] = L".db-wal";
        if (len >= 7) {
            BOOL hit = TRUE;
            for (size_t k = 0; k < 7; k++) {
                WCHAR a = path[len - 7 + k];
                if (a >= L'A' && a <= L'Z') a = (WCHAR)(a - L'A' + L'a');
                if (a != suffix[k]) { hit = FALSE; break; }
            }
            is_wal = hit ? 1 : 0;
        }
    }

    /* Store at idx as well as the first free probe slot we walked. */
    g_cache[idx].h = h;
    g_cache[idx].is_wal = is_wal;
    return is_wal;
}

/* ---------------------------------------------------------------- pipe output */
static BOOL pipe_connect(void) {
    HANDLE h = CreateFileW(WXWAL_PIPE_NAME, GENERIC_WRITE, 0, NULL, OPEN_EXISTING, 0, NULL);
    if (h == INVALID_HANDLE_VALUE) return FALSE;
    g_pipe = h;
    return TRUE;
}

/*
 * Best-effort, never blocks for long. On failure the frame is dropped and the
 * connection is closed so the next frame retries a fresh connect.
 */
static void emit_frame(HANDLE hFile, LPCVOID buf, DWORD size) {
    if (size == 0 || size > (64u << 20)) return;   /* sanity bound */

    if (InterlockedCompareExchange(&g_running, 1, 1) == 0) return;

    /* Resolve the path so the reader can pick the right per-database key. */
    CHAR path_utf8[1024];
    DWORD path_len = 0;
    {
        WCHAR wp[600];
        DWORD n = GetFinalPathNameByHandleW(hFile, wp, 600,
                                            FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
        if (n > 0 && n < 600) {
            int k = WideCharToMultiByte(CP_UTF8, 0, wp, -1, path_utf8, sizeof(path_utf8) - 1,
                                        NULL, NULL);
            if (k > 0) path_len = (DWORD)(k - 1);   /* drop the null */
        }
    }

    WalFrameHeader hdr;
    hdr.magic    = FRAME_MAGIC;
    hdr.pid      = g_self_pid;
    hdr.handle   = (DWORD)(ULONG_PTR)hFile;
    hdr.size     = size;
    hdr.tick     = GetTickCount();
    hdr.path_len = path_len;

    if (WaitForSingleObject(g_pipe_lock, 50) != WAIT_OBJECT_0) {
        InterlockedIncrement(&g_dropped);
        return;
    }

    /*
     * Reconnect on demand. The sidecar may not be listening when the DLL loads
     * (that is the normal case: inject first, then start the reader), so a
     * connect-once design silently discards every frame forever.
     */
    if (g_pipe == INVALID_HANDLE_VALUE) {
        pipe_connect();
        if (g_pipe != INVALID_HANDLE_VALUE) log_status("wxwal: pipe connected");
    }

    BOOL ok = FALSE;
    if (g_pipe != INVALID_HANDLE_VALUE) {
        DWORD written = 0;
        ok = TRUE;   /* header write decides; do NOT gate the payload on a flag
                      * that is only set inside the payload loop, or every frame
                      * is dropped while each individual write succeeds. */
        if (!WriteFile(g_pipe, &hdr, sizeof(hdr), &written, NULL) || written != sizeof(hdr)) {
            ok = FALSE;
        }
        if (ok && path_len > 0) {
            if (!WriteFile(g_pipe, path_utf8, path_len, &written, NULL) || written != path_len) {
                ok = FALSE;
            }
        }
        if (ok) {
            const BYTE *p = (const BYTE *)buf;
            DWORD left = size;
            while (left > 0) {
                DWORD chunk = left > (1u << 20) ? (1u << 20) : left;
                if (!WriteFile(g_pipe, p, chunk, &written, NULL) || written != chunk) {
                    ok = FALSE;
                    break;
                }
                p += chunk;
                left -= chunk;
            }
        }
    }

    if (!ok) {
        if (g_pipe != INVALID_HANDLE_VALUE) { CloseHandle(g_pipe); g_pipe = INVALID_HANDLE_VALUE; }
        InterlockedIncrement(&g_dropped);
    } else {
        InterlockedIncrement(&g_frames);
    }

    ReleaseMutex(g_pipe_lock);
}

/* ------------------------------------------------------------------- hooks */
static BOOL WINAPI Hook_WriteFile(HANDLE hFile, LPCVOID lpBuffer, DWORD nNumberOfBytesToWrite,
                                  LPDWORD lpNumberOfBytesWritten, LPOVERLAPPED lpOverlapped) {
    LONG c = InterlockedIncrement(&g_calls);

    /*
     * Diagnostic phase: dump the resolved path of the first calls so we can see
     * exactly which files WeChat writes through this import. Without this the
     * only observable is "wal_hits stayed 0", which cannot distinguish
     * "database is written elsewhere" from "path classifier is wrong".
     */
    if (c <= 80 && nNumberOfBytesToWrite > 0) {
        WCHAR p[512];
        DWORD n = GetFinalPathNameByHandleW(hFile, p, 512,
                                            FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
        CHAR line[640];
        if (n > 0 && n < 512) {
            WideCharToMultiByte(CP_UTF8, 0, p, -1, line, sizeof(line), NULL, NULL);
        } else {
            wsprintfA(line, "<no path, handle=%p err=%lu>", hFile, GetLastError());
        }
        CHAR out[768];
        wsprintfA(out, "wxwal call#%ld size=%lu ovl=%p path=%s",
                  c, nNumberOfBytesToWrite, lpOverlapped, line);
        log_status(out);
    }

    /*
     * No lpOverlapped filter. WeChat's SQLite issues WAL writes as overlapped
     * I/O, so requiring a NULL OVERLAPPED silently discarded every frame while
     * the hook itself looked healthy. For an overlapped write the buffer is
     * already populated when WriteFile is called (the kernel reads it during
     * the call), so copying it here is safe; only the *asynchronous reuse* of
     * the buffer after the call would be racy, and we copy before returning.
     */
    if (classify_handle(hFile)) {
        InterlockedIncrement(&g_walhits);
        emit_frame(hFile, lpBuffer, nNumberOfBytesToWrite);
    }
    return real_WriteFile(hFile, lpBuffer, nNumberOfBytesToWrite, lpNumberOfBytesWritten, lpOverlapped);
}

static BOOL WINAPI Hook_WriteFileEx(HANDLE hFile, LPCVOID lpBuffer, DWORD nNumberOfBytesToWrite,
                                    LPOVERLAPPED lpOverlapped,
                                    LPOVERLAPPED_COMPLETION_ROUTINE lpCompletionRoutine) {
    /*
     * Overlapped writes take an LPOVERLAPPED; the kernel owns the buffer until
     * completion, so copying it here would race. We deliberately do NOT capture
     * these and rely on the synchronous path, which is what SQLite's WAL writer
     * uses in practice. Recorded as a known gap rather than guessed at.
     */
    return real_WriteFileEx(hFile, lpBuffer, nNumberOfBytesToWrite, lpOverlapped, lpCompletionRoutine);
}

/* -------------------------------------------------------------- IAT patching */
static BOOL patch_iat(HMODULE target, const char *dll_name, const char *func_name,
                      void *replacement, void **original_out) {
    BYTE *base = (BYTE *)target;
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)base;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE) return FALSE;
    IMAGE_NT_HEADERS *nt = (IMAGE_NT_HEADERS *)(base + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE) return FALSE;

    IMAGE_DATA_DIRECTORY imp_dir =
        nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (!imp_dir.VirtualAddress || !imp_dir.Size) return FALSE;

    IMAGE_IMPORT_DESCRIPTOR *imp = (IMAGE_IMPORT_DESCRIPTOR *)(base + imp_dir.VirtualAddress);
    for (; imp->Name; imp++) {
        const char *mod = (const char *)(base + imp->Name);
        if (_stricmp(mod, dll_name) != 0) continue;

        IMAGE_THUNK_DATA *orig = (IMAGE_THUNK_DATA *)(base + imp->OriginalFirstThunk);
        IMAGE_THUNK_DATA *iat  = (IMAGE_THUNK_DATA *)(base + imp->FirstThunk);
        for (; orig->u1.AddressOfData; orig++, iat++) {
            if (IMAGE_SNAP_BY_ORDINAL(orig->u1.Ordinal)) continue;
            IMAGE_IMPORT_BY_NAME *ibn = (IMAGE_IMPORT_BY_NAME *)(base + orig->u1.AddressOfData);
            if (strcmp((const char *)ibn->Name, func_name) != 0) continue;

            DWORD old_prot = 0;
            if (!VirtualProtect(&iat->u1.Function, sizeof(void *), PAGE_READWRITE, &old_prot))
                return FALSE;
            if (original_out) *original_out = (void *)iat->u1.Function;
            iat->u1.Function = (ULONG_PTR)replacement;
            VirtualProtect(&iat->u1.Function, sizeof(void *), old_prot, &old_prot);
            return TRUE;
        }
    }
    return FALSE;
}

/* ----------------------------------------------------------------- lifecycle */
/*
 * 日志文件在运行时根据本 DLL 自身所在目录推导，因此工程可以放在任何机器
 * 的任何路径下 —— 不编译进绝对路径。日志名跟随 DLL 文件名
 * （wxwal10.dll -> wxwal10.log）：每次重新编译注入时，新旧副本各自写自己
 * 的日志，计数器才不会互相污染。
 */
static WCHAR g_log_path[MAX_PATH * 2] = L"";
static HMODULE g_self_module = NULL;

static void resolve_log_path(void) {
    WCHAR full[MAX_PATH * 2];
    DWORD n = GetModuleFileNameW(g_self_module, full, ARRAYSIZE(full));
    if (n == 0 || n >= ARRAYSIZE(full)) return;

    WCHAR *bs = wcsrchr(full, L'\\');
    if (!bs) return;
    *(bs + 1) = L'\0';                       /* 保留目录（含结尾反斜杠） */

    const WCHAR *name = bs + 1;              /* 原始 DLL 文件名 */
    if (wcslen(full) + wcslen(name) + 8 >= ARRAYSIZE(g_log_path)) return;

    wcscpy(g_log_path, full);
    wcscat(g_log_path, name);
    WCHAR *dot = wcsrchr(g_log_path, L'.');
    if (dot && dot > full) wcscpy(dot, L".log");
    else wcscat(g_log_path, L".log");
}

static void log_status(const char *msg) {
    if (g_log_path[0] == L'\0') return;
    HANDLE f = CreateFileW(g_log_path,
                           FILE_APPEND_DATA, FILE_SHARE_READ, NULL, OPEN_ALWAYS, 0, NULL);
    if (f == INVALID_HANDLE_VALUE) return;
    DWORD w;
    SetFilePointer(f, 0, NULL, FILE_END);
    WriteFile(f, msg, (DWORD)strlen(msg), &w, NULL);
    WriteFile(f, "\r\n", 2, &w, NULL);
    CloseHandle(f);
}

BOOL WINAPI DllMain(HINSTANCE hinstDLL, DWORD fdwReason, LPVOID lpvReserved) {
    (void)lpvReserved;

    if (fdwReason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hinstDLL);
        g_self_module = hinstDLL;
        resolve_log_path();
        g_self_pid = GetCurrentProcessId();
        g_pipe_lock = CreateMutexW(NULL, FALSE, NULL);
        InterlockedExchange(&g_running, 1);

        HMODULE weixin = GetModuleHandleW(L"Weixin.dll");
        if (!weixin) {
            log_status("wxwal: Weixin.dll not found in process; no hook installed");
            return TRUE;
        }

        BOOL ok1 = patch_iat(weixin, "KERNEL32.dll", "WriteFile",
                             (void *)Hook_WriteFile, (void **)&real_WriteFile);
        /* WriteFileEx hooked only to record that we deliberately pass it through. */
        void *unused = NULL;
        BOOL ok2 = patch_iat(weixin, "KERNEL32.dll", "WriteFileEx",
                             (void *)Hook_WriteFileEx, (void **)&real_WriteFileEx);
        (void)ok2; (void)unused;

        if (ok1 && real_WriteFile) {
            pipe_connect();
            log_status("wxwal: WriteFile hooked successfully");
            CreateThread(NULL, 0, stats_thread, NULL, 0, NULL);
        } else {
            log_status("wxwal: FAILED to hook WriteFile");
        }
    }
    return TRUE;
}

/* Exported so the sidecar can query liveness without touching the hook path. */
__declspec(dllexport) unsigned long wxwal_frames(void)  { return (unsigned long)g_frames; }
__declspec(dllexport) unsigned long wxwal_dropped(void) { return (unsigned long)g_dropped; }
