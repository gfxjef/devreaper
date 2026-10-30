"""In-process Windows reads through ctypes: commit charge, kernel pools, pool tags and the process table.
None of these start a process (each one would leak kernel memory on machines with the token leak), and
none needs admin. Every function returns None when the call is unavailable, so callers degrade quietly."""
import ctypes
import sys
import threading
from ctypes import wintypes

MB = 2**20
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
SYSTEM_POOL_TAG_INFORMATION = 22
TH32CS_SNAPPROCESS = 0x2
PDH_FMT_LARGE = 0x400
WINDOWS = sys.platform == "win32"


class _PerfInfo(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("CommitTotal", ctypes.c_size_t), ("CommitLimit", ctypes.c_size_t),
                ("CommitPeak", ctypes.c_size_t), ("PhysicalTotal", ctypes.c_size_t),
                ("PhysicalAvailable", ctypes.c_size_t), ("SystemCache", ctypes.c_size_t),
                ("KernelTotal", ctypes.c_size_t), ("KernelPaged", ctypes.c_size_t),
                ("KernelNonpaged", ctypes.c_size_t), ("PageSize", ctypes.c_size_t),
                ("HandleCount", wintypes.DWORD), ("ProcessCount", wintypes.DWORD), ("ThreadCount", wintypes.DWORD)]


class _PoolTag(ctypes.Structure):
    _fields_ = [("Tag", ctypes.c_char * 4),
                ("PagedAllocs", ctypes.c_ulong), ("PagedFrees", ctypes.c_ulong), ("PagedUsed", ctypes.c_size_t),
                ("NonPagedAllocs", ctypes.c_ulong), ("NonPagedFrees", ctypes.c_ulong),
                ("NonPagedUsed", ctypes.c_size_t)]


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


class _PdhValue(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("pad", wintypes.DWORD), ("largeValue", ctypes.c_longlong)]


def perf_info():
    """{'commit_mb', 'commit_limit_mb', 'paged_mb', 'nonpaged_mb'}. Paged is the pool's virtual size, not RAM."""
    if not WINDOWS:
        return None
    p = _PerfInfo()
    p.cb = ctypes.sizeof(p)
    try:
        if not ctypes.WinDLL("psapi").GetPerformanceInfo(ctypes.byref(p), p.cb):
            return None
    except OSError:
        return None
    mb = lambda pages: round(pages * p.PageSize / MB, 1)
    return {"commit_mb": mb(p.CommitTotal), "commit_limit_mb": mb(p.CommitLimit),
            "paged_mb": mb(p.KernelPaged), "nonpaged_mb": mb(p.KernelNonpaged)}


class ResidentPagedPool:
    """'\\Memory\\Pool Paged Resident Bytes' through PDH: the part of the paged pool that sits in RAM.
    The query is opened once and kept; each read is one collect."""

    def __init__(self):
        self._lock = threading.Lock()
        self._query = self._counter = None
        self._failed = not WINDOWS

    def _open(self):
        pdh = ctypes.WinDLL("pdh")
        query, counter = ctypes.c_void_p(), ctypes.c_void_p()
        if pdh.PdhOpenQueryW(None, None, ctypes.byref(query)) != 0:
            raise OSError("PdhOpenQuery")
        if pdh.PdhAddEnglishCounterW(query, "\\Memory\\Pool Paged Resident Bytes", None, ctypes.byref(counter)) != 0:
            pdh.PdhCloseQuery(query)
            raise OSError("PdhAddEnglishCounter")
        self._pdh, self._query, self._counter = pdh, query, counter

    def read_mb(self):
        with self._lock:
            if self._failed:
                return None
            try:
                if self._query is None:
                    self._open()
                if self._pdh.PdhCollectQueryData(self._query) != 0:
                    return None
                value = _PdhValue()
                if self._pdh.PdhGetFormattedCounterValue(self._counter, PDH_FMT_LARGE, None, ctypes.byref(value)):
                    return None
                return round(value.largeValue / MB, 1)
            except (OSError, AttributeError):
                self._failed = True
                return None


_pool_buffer = {"size": 1 << 20}


def pool_tags():
    """{tag: (paged_bytes, paged_objects, nonpaged_bytes, nonpaged_objects)} from SystemPoolTagInformation."""
    if not WINDOWS:
        return None
    try:
        ntdll = ctypes.WinDLL("ntdll")
    except OSError:
        return None
    size = _pool_buffer["size"]
    for _ in range(6):
        buf = ctypes.create_string_buffer(size)
        needed = ctypes.c_ulong(0)
        status = ntdll.NtQuerySystemInformation(SYSTEM_POOL_TAG_INFORMATION, buf, size, ctypes.byref(needed))
        status &= 0xFFFFFFFF
        if status == STATUS_INFO_LENGTH_MISMATCH:
            size = max(size * 2, needed.value + 4096)
            continue
        if status != 0:
            return None
        _pool_buffer["size"] = size
        count = ctypes.c_ulong.from_buffer(buf, 0).value
        # the array starts after the ULONG count, aligned to pointer size
        entries = (_PoolTag * count).from_buffer(buf, ctypes.sizeof(ctypes.c_size_t))
        return {bytes(t.Tag).decode("latin-1"): (t.PagedUsed, (t.PagedAllocs - t.PagedFrees) & 0xFFFFFFFF,
                                                 t.NonPagedUsed, (t.NonPagedAllocs - t.NonPagedFrees) & 0xFFFFFFFF)
                for t in entries}
    return None


def process_table():
    """{pid: (ppid, exe name)} for every process, in one Toolhelp snapshot (~10 ms for ~400 processes)."""
    if not WINDOWS:
        return None
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return None
    out = {}
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out
