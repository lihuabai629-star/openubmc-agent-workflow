"""Windows current-user file boundary for local Runtime state and credentials.

Only built-in Win32 APIs and icacls are used; credential bytes never cross a
subprocess boundary.  Linux callers do not import or execute these primitives.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import hashlib
import os
from pathlib import Path
import subprocess
import sys


class WindowsPrivateError(ValueError):
    pass


def _apis():
    if sys.platform != "win32":
        raise WindowsPrivateError("Windows private storage requires native Windows")
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                           wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi.EqualSid.restype = wintypes.BOOL
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
                                             ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                             ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                             ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int]
    advapi.GetAclInformation.restype = wintypes.BOOL
    advapi.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetAce.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    return advapi, kernel, wintypes


def _current_user_sid(advapi, kernel, wintypes) -> tuple[str, ctypes.Array]:
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise WindowsPrivateError("Cannot inspect the current Windows user token")
    try:
        required = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi.GetTokenInformation(token, 1, buffer, required, ctypes.byref(required)):
            raise WindowsPrivateError("Cannot inspect the current Windows user token")
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = ctypes.c_void_p()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise WindowsPrivateError("Cannot inspect the current Windows user SID")
        try:
            return ctypes.wstring_at(text), buffer
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.CloseHandle(token)


def _sid_text(advapi, kernel, sid: int) -> str:
    text = ctypes.c_void_p()
    if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise WindowsPrivateError("Cannot inspect a Windows access entry")
    try:
        return ctypes.wstring_at(text)
    finally:
        kernel.LocalFree(text)


def verify_private_path(path: Path, *, safe_parent: bool = False) -> None:
    """Reject a foreign owner, a null DACL, or effective access by other users.

    A parent used only to create a private child may grant read/traverse to
    others, but cannot grant write, delete, ownership or ACL changes.
    """
    path = Path(path)
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise WindowsPrivateError("Private Windows paths cannot be reparse links")
    advapi, kernel, wintypes = _apis()
    user_sid, user_buffer = _current_user_sid(advapi, kernel, wintypes)
    del user_buffer  # The string identity has been copied from the token.
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = advapi.GetNamedSecurityInfoW(str(path), 1, 1 | 4,
                                          ctypes.byref(owner), None, ctypes.byref(dacl),
                                          None, ctypes.byref(descriptor))
    if result:
        raise WindowsPrivateError("Cannot inspect Windows file ownership and access")
    try:
        if not owner.value or _sid_text(advapi, kernel, owner) != user_sid or not dacl.value:
            raise WindowsPrivateError("Windows private path has an unsafe owner or access list")

        class AclSize(ctypes.Structure):
            _fields_ = [("ace_count", wintypes.DWORD), ("bytes_in_use", wintypes.DWORD),
                        ("bytes_free", wintypes.DWORD)]

        size = AclSize()
        if not advapi.GetAclInformation(dacl, ctypes.byref(size), ctypes.sizeof(size), 2):
            raise WindowsPrivateError("Cannot inspect Windows access entries")
        trusted = {user_sid, "S-1-3-4", "S-1-5-18", "S-1-5-32-544"}  # Owner Rights, SYSTEM, Administrators.
        # Generic all/write, delete, ACL/owner changes, and directory writes.
        unsafe_parent_rights = (1 << 28) | (1 << 30) | (13 << 16) | 342
        for index in range(size.ace_count):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)):
                raise WindowsPrivateError("Cannot inspect Windows access entries")
            kind = ctypes.c_ubyte.from_address(ace.value).value
            if kind == 1:  # Explicit or inherited deny entry.
                continue
            if kind != 0:  # Unknown or conditional allow entry: fail closed.
                raise WindowsPrivateError("Windows private path has unsupported access entries")
            mask = ctypes.c_uint32.from_address(ace.value + 4).value
            sid = _sid_text(advapi, kernel, ace.value + 8)
            if sid not in trusted and mask and (not safe_parent or mask & unsafe_parent_rights):
                raise WindowsPrivateError("Windows private path grants access to another user")
    finally:
        kernel.LocalFree(descriptor)


def _inherited_read_snapshot(path: Path) -> str:
    """Identify the one existing ACL shape that setup can repair explicitly."""
    path = Path(path)
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise WindowsPrivateError("private_root_reparse_link")
    if not path.is_dir():
        raise WindowsPrivateError("private_root_not_directory")
    advapi, kernel, wintypes = _apis()
    user_sid, user_buffer = _current_user_sid(advapi, kernel, wintypes)
    del user_buffer
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = advapi.GetNamedSecurityInfoW(str(path), 1, 1 | 4,
                                          ctypes.byref(owner), None, ctypes.byref(dacl),
                                          None, ctypes.byref(descriptor))
    if result:
        raise WindowsPrivateError("private_root_inspection_failed")
    try:
        if not owner.value or _sid_text(advapi, kernel, owner) != user_sid or not dacl.value:
            raise WindowsPrivateError("private_root_unsafe_owner")

        class AclSize(ctypes.Structure):
            _fields_ = [("ace_count", wintypes.DWORD), ("bytes_in_use", wintypes.DWORD),
                        ("bytes_free", wintypes.DWORD)]

        size = AclSize()
        if not advapi.GetAclInformation(dacl, ctypes.byref(size), ctypes.sizeof(size), 2):
            raise WindowsPrivateError("private_root_inspection_failed")
        trusted = {user_sid, "S-1-3-4", "S-1-5-18", "S-1-5-32-544"}
        read_only = 0x80000000 | 0x20000000 | 0x001200A9
        inherited_outside_read = False
        for index in range(size.ace_count):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)):
                raise WindowsPrivateError("private_root_inspection_failed")
            kind = ctypes.c_ubyte.from_address(ace.value).value
            flags = ctypes.c_ubyte.from_address(ace.value + 1).value
            if kind == 1:
                continue
            if kind != 0:
                raise WindowsPrivateError("private_root_unsupported_access")
            mask = ctypes.c_uint32.from_address(ace.value + 4).value
            sid = _sid_text(advapi, kernel, ace.value + 8)
            if sid in trusted or not mask:
                continue
            if not flags & 0x10:
                raise WindowsPrivateError("private_root_explicit_outside_access")
            if mask & ~read_only:
                raise WindowsPrivateError("private_root_outside_write_access")
            inherited_outside_read = True
        if not inherited_outside_read:
            raise WindowsPrivateError("private_root_not_repairable")
        advapi.GetSecurityDescriptorLength.argtypes = [ctypes.c_void_p]
        advapi.GetSecurityDescriptorLength.restype = wintypes.DWORD
        length = advapi.GetSecurityDescriptorLength(descriptor)
        if not length:
            raise WindowsPrivateError("private_root_inspection_failed")
        identity = path.stat()
        digest = hashlib.sha256()
        digest.update(str(path.absolute()).casefold().encode("utf-8"))
        digest.update(str((identity.st_dev, identity.st_ino)).encode("ascii"))
        digest.update(ctypes.string_at(descriptor, length))
        return digest.hexdigest()
    finally:
        kernel.LocalFree(descriptor)


def private_directory_recovery_status(path: Path) -> dict[str, str]:
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return {"status": "ready"}
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        return {"status": "blocked", "reason_code": "private_root_reparse_link"}
    if not path.is_dir():
        return {"status": "blocked", "reason_code": "private_root_not_directory"}
    try:
        verify_private_path(path)
        return {"status": "ready"}
    except (WindowsPrivateError, OSError):
        pass
    try:
        return {"status": "repairable", "snapshot_token": _inherited_read_snapshot(path)}
    except (WindowsPrivateError, OSError) as error:
        code = str(error) if str(error).startswith("private_root_") else "private_root_inspection_failed"
        return {"status": "blocked", "reason_code": code}


def repair_inherited_read_directory(path: Path, *, expected_token: str) -> None:
    path = Path(path)
    if not isinstance(expected_token, str) or len(expected_token) != 64:
        raise WindowsPrivateError("private_root_changed")
    before = path.stat(follow_symlinks=False)
    if _inherited_read_snapshot(path) != expected_token:
        raise WindowsPrivateError("private_root_changed")
    if path.stat(follow_symlinks=False) != before:
        raise WindowsPrivateError("private_root_changed")
    _harden(path, directory=True)
    after = path.stat(follow_symlinks=False)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise WindowsPrivateError("private_root_changed")
    verify_private_path(path)


def _harden(path: Path, *, directory: bool) -> None:
    advapi, kernel, wintypes = _apis()
    sid, _buffer = _current_user_sid(advapi, kernel, wintypes)
    suffix = ":(OI)(CI)F" if directory else ":F"
    result = subprocess.run(
        ["icacls.exe", str(path), "/inheritance:r", "/grant:r",
         f"*{sid}{suffix}", f"*S-1-5-18{suffix}", f"*S-1-5-32-544{suffix}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=1 << 27, timeout=10,  # CREATE_NO_WINDOW
    )
    if result.returncode:
        raise WindowsPrivateError("Cannot establish current-user Windows access")
    verify_private_path(path)


def ensure_private_directory(path: Path) -> None:
    path = Path(path)
    if path.exists():
        if not path.is_dir():
            raise WindowsPrivateError("Private configuration path is not a directory")
        verify_private_path(path)
        return
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise WindowsPrivateError("Private configuration path cannot be a reparse link")
    parent = path.parent
    if parent == path:
        raise WindowsPrivateError("Cannot establish a private configuration directory")
    if not parent.exists():
        ensure_private_directory(parent)
    else:
        verify_private_path(parent, safe_parent=True)
    try:
        path.mkdir()
    except FileExistsError:
        verify_private_path(path)
        return
    try:
        _harden(path, directory=True)
    except BaseException:
        path.rmdir()
        raise


def harden_new_file(path: Path) -> None:
    _harden(Path(path), directory=False)


@contextmanager
def locked_file(descriptor: int, *, blocking: bool = True):
    """Hold one mandatory Windows byte-range lock across the caller's update."""
    if sys.platform != "win32":
        raise WindowsPrivateError("Windows file locking requires native Windows")
    import msvcrt

    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.fstat(descriptor).st_size == 0:
        os.write(descriptor, b"\0")
        os.fsync(descriptor)
    os.lseek(descriptor, 0, os.SEEK_SET)
    try:
        msvcrt.locking(descriptor, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        raise WindowsPrivateError("Windows configuration lock is held by another process") from exc
    try:
        yield
    finally:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
