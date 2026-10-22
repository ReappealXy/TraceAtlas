"""Windows kernel job: descendants die with the owner and share a memory budget."""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

_MEMORY_EXIT_STATUSES = {
    0xC0000007,  # STATUS_PAGEFILE_QUOTA
    0xC0000017,  # STATUS_NO_MEMORY
    0xC000012D,  # STATUS_COMMITMENT_LIMIT
}
_JOB_MESSAGES = {
    1: 'end_of_job_time', 2: 'end_of_process_time', 3: 'active_process_limit',
    4: 'active_process_zero', 6: 'new_process', 7: 'exit_process',
    8: 'abnormal_exit_process', 9: 'process_memory_limit', 10: 'job_memory_limit',
    11: 'notification_limit',
}


def is_memory_limit_exit(returncode):
    """Recognize explicit memory NTSTATUS values, signed or unsigned.

    These codes establish memory exhaustion, but do not distinguish the job's
    budget from the system's commit limit. Ordinary application exit codes,
    including Win32 ERROR_NOT_ENOUGH_MEMORY (8), are deliberately excluded.
    """
    return isinstance(returncode, int) and (returncode & 0xFFFFFFFF) in _MEMORY_EXIT_STATUSES


class WorkerJob:
    def __init__(self, process, memory_bytes=4 * 1024**3):
        self.handle = None
        self.completion_port = None
        self._memory_limit_exceeded = False
        if os.name != 'nt':
            return

        class BasicLimits(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                        ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                        ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
                        ('SchedulingClass', wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ('ReadOperationCount', 'WriteOperationCount',
                        'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', BasicLimits), ('IoInfo', IoCounters),
                        ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

        class AssociateCompletionPort(ctypes.Structure):
            _fields_ = [('CompletionKey', ctypes.c_void_p), ('CompletionPort', wintypes.HANDLE)]

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CreateIoCompletionPort.argtypes = [wintypes.HANDLE, wintypes.HANDLE,
                                                ctypes.c_size_t, wintypes.DWORD]
        kernel.CreateIoCompletionPort.restype = wintypes.HANDLE
        kernel.GetQueuedCompletionStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
                                                     ctypes.POINTER(ctypes.c_size_t),
                                                     ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD]
        kernel.GetQueuedCompletionStatus.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel = kernel
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = handle
        port = kernel.CreateIoCompletionPort(wintypes.HANDLE(-1), None, 0, 1)
        if not port:
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error
        self.completion_port = port
        association = AssociateCompletionPort(1, port)
        limits = ExtendedLimits()
        # KILL_ON_JOB_CLOSE | JOB_MEMORY (committed bytes across the worker process tree).
        limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x0200
        limits.JobMemoryLimit = memory_bytes
        # Associate before any worker joins so its first events are observable.
        if (not kernel.SetInformationJobObject(handle, 7, ctypes.byref(association), ctypes.sizeof(association))
                or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not kernel.AssignProcessToJobObject(handle, wintypes.HANDLE(int(process._handle)))):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def poll_notifications(self):
        """Drain queued kernel notices without blocking; remember memory breaches.

        Windows does not guarantee delivery of hard-limit notifications. A
        missing notice must not be interpreted as proof of sufficient memory.
        """
        notices = []
        if not self.completion_port:
            return notices
        while True:
            message = wintypes.DWORD()
            key = ctypes.c_size_t()
            process_id = ctypes.c_void_p()
            if not self.kernel.GetQueuedCompletionStatus(self.completion_port, ctypes.byref(message),
                                                          ctypes.byref(key), ctypes.byref(process_id), 0):
                error = ctypes.get_last_error()
                if error == 258:  # WAIT_TIMEOUT: no queued packet.
                    break
                raise ctypes.WinError(error)
            if key.value != 1:
                continue
            if message.value in {9, 10}:
                self._memory_limit_exceeded = True
            notices.append({'code': message.value, 'name': _JOB_MESSAGES.get(message.value, 'unknown'),
                            'pid': process_id.value})
        return notices

    @property
    def memory_limit_exceeded(self):
        self.poll_notifications()
        return self._memory_limit_exceeded

    def close(self):
        try:
            self.poll_notifications()
        except OSError:
            # Cleanup must still release both handles, including after a failed
            # constructor, and must not replace the original failure.
            pass
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
        if self.completion_port:
            self.kernel.CloseHandle(self.completion_port)
            self.completion_port = None
