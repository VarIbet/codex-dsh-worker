"""Keychain access without secrets in argv, environment, or subprocess output."""
import ctypes as C
import os
import getpass
from pathlib import Path

SERVICE = b"Codex-DSH-Worker"
ACCOUNT = b"deepseek-api"

def library():
    s = C.CDLL('/System/Library/Frameworks/Security.framework/Security')
    s.SecKeychainFindGenericPassword.argtypes = [C.c_void_p, C.c_uint32, C.c_char_p, C.c_uint32, C.c_char_p, C.POINTER(C.c_uint32), C.POINTER(C.c_void_p), C.c_void_p]
    s.SecKeychainAddGenericPassword.argtypes = [C.c_void_p, C.c_uint32, C.c_char_p, C.c_uint32, C.c_char_p, C.c_uint32, C.c_char_p, C.c_void_p]
    s.SecKeychainItemFreeContent.argtypes = [C.c_void_p, C.c_void_p]
    return s

def keychain_get():
    s = library()
    n, ptr = C.c_uint32(), C.c_void_p()
    rc = s.SecKeychainFindGenericPassword(None, len(SERVICE), SERVICE, len(ACCOUNT), ACCOUNT, C.byref(n), C.byref(ptr), None)
    if rc != 0:
        raise RuntimeError('Keychain unavailable (OSStatus %s)' % rc)
    try:
        return C.string_at(ptr, n.value).decode()
    finally:
        s.SecKeychainItemFreeContent(None, ptr)

def get(root):
    store = root / 'config' / 'api-key'
    if store.exists():
        if store.is_symlink() or store.stat().st_mode & 0o077:
            raise RuntimeError('Unsafe credential file permissions')
        return store.read_text().strip()
    return keychain_get()

def configure(root, key_file=None, allow_file_fallback=False):
    # Hidden input or an explicitly supplied file; no secret in process arguments.
    raw = Path(key_file).expanduser().read_text().strip() if key_file else getpass.getpass('DeepSeek API key (hidden): ').strip()
    if not raw.startswith('sk-') or any(c.isspace() for c in raw) or len(raw) < 20:
        raise RuntimeError('Supply exactly one valid API key')
    try:
        old = keychain_get()
    except RuntimeError:
        old = None
    if old is not None:
        if old != raw:
            raise RuntimeError('Existing Keychain item differs; refusing to replace it')
        return 'macOS Keychain (existing item verified)'
    key = raw.encode()
    rc = library().SecKeychainAddGenericPassword(None, len(SERVICE), SERVICE, len(ACCOUNT), ACCOUNT, len(key), key, None)
    if rc == 0 and keychain_get() == raw:
        return 'macOS Keychain'
    if not allow_file_fallback:
        raise RuntimeError('Keychain storage failed (OSStatus %s); explicit --allow-file-fallback required for file storage' % rc)
    store = root / 'config' / 'api-key'
    fd = os.open(str(store), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(raw)
    return 'restricted file (0600); Keychain OSStatus %s' % rc
