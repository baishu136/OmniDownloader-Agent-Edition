"""Windows kernel Job Objects: kill child processes if their worker/server dies."""
import os


def bind_job(pid):
    if os.name != 'nt': return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)

    class Basic(ctypes.Structure):
        _fields_ = [('ProcessTime',ctypes.c_int64),('JobTime',ctypes.c_int64),('Flags',wintypes.DWORD),
                    ('MinWorking',ctypes.c_size_t),('MaxWorking',ctypes.c_size_t),('ActiveLimit',wintypes.DWORD),
                    ('Affinity',ctypes.c_size_t),('Priority',wintypes.DWORD),('Scheduling',wintypes.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(name,ctypes.c_uint64) for name in ('ReadOps','WriteOps','OtherOps','ReadBytes','WriteBytes','OtherBytes')]
    class Extended(ctypes.Structure):
        _fields_ = [('Basic',Basic),('IO',IO),('ProcessMemory',ctypes.c_size_t),('JobMemory',ctypes.c_size_t),
                    ('PeakProcessMemory',ctypes.c_size_t),('PeakJobMemory',ctypes.c_size_t)]
    kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype=wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    job=kernel.CreateJobObjectW(None,None)
    if not job:raise ctypes.WinError(ctypes.get_last_error())
    info=Extended()
    info.Basic.Flags=0x00002000 # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    process=kernel.OpenProcess(0x0100 | 0x0001,False,pid) # SET_QUOTA | TERMINATE
    try:
        if not kernel.SetInformationJobObject(job,9,ctypes.byref(info),ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not process or not kernel.AssignProcessToJobObject(job,process):
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        kernel.CloseHandle(job)
        raise
    finally:
        if process:kernel.CloseHandle(process)
    return job


def close_job(handle):
    if handle is not None:
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        kernel.CloseHandle(handle)
