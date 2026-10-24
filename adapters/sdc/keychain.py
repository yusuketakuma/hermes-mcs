"""Keep customer Connector credentials in one noninteractive macOS Keychain item."""
from contextlib import contextmanager
import ctypes
import sys
import threading
import uuid

from .protocol import strict_json

SERVICE = 'local.mcs.sdc.device-credential.v1'
MAX_BYTES = 4096
SECURITY_FRAMEWORK = '/System/Library/Frameworks/Security.framework/Security'
CORE_FOUNDATION_FRAMEWORK = '/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation'
_UI_LOCK = threading.RLock()
_DUPLICATE = -25299
_CODES = frozenset({'SDC_KEYCHAIN_NOT_FOUND', 'SDC_KEYCHAIN_LOCKED', 'SDC_KEYCHAIN_AUTH_FAILED',
                    'SDC_KEYCHAIN_UNAVAILABLE', 'SDC_KEYCHAIN_INVALID_VALUE', 'SDC_KEYCHAIN_INVALID_ACCOUNT'})


class KeychainError(ValueError):
    def __init__(self, code):
        self.code = code if code in _CODES else 'SDC_KEYCHAIN_UNAVAILABLE'
        super().__init__(self.code)


def _status(code):
    if type(code) is not int:
        raise KeychainError('SDC_KEYCHAIN_UNAVAILABLE')
    if code == 0:
        return
    message = {-25300: 'SDC_KEYCHAIN_NOT_FOUND', -25308: 'SDC_KEYCHAIN_LOCKED', -25315: 'SDC_KEYCHAIN_LOCKED',
               -25293: 'SDC_KEYCHAIN_AUTH_FAILED', -128: 'SDC_KEYCHAIN_AUTH_FAILED'}.get(code, 'SDC_KEYCHAIN_UNAVAILABLE')
    raise KeychainError(message)


def _blob(raw):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_BYTES:
        raise KeychainError('SDC_KEYCHAIN_INVALID_VALUE')
    try:
        strict_json(raw)
    except (ValueError, TypeError, RecursionError):
        raise KeychainError('SDC_KEYCHAIN_INVALID_VALUE') from None
    return raw


class KeychainCredentialStore:
    """Own only SERVICE plus one locally generated UUIDv4 account.

    native is an injectable provider with copy(account)->(OSStatus, bytes),
    add(account, bytes), update(account, bytes), and delete(account)->OSStatus.
    The caller retains credential schema validation and the private root lock.
    """
    def __init__(self, account, *, native=None):
        try:
            parsed = uuid.UUID(account) if isinstance(account, str) else None
            valid = parsed is not None and str(parsed) == account and parsed.version == 4
        except ValueError:
            valid = False
        if not valid:
            raise KeychainError('SDC_KEYCHAIN_INVALID_ACCOUNT')
        self.account, self.native = account, native

    def _call(self, method, *args):
        try:
            if self.native is None:
                self.native = _MacKeychain()
            return getattr(self.native, method)(self.account, *args)
        except (KeychainError, TimeoutError):
            raise
        except Exception:
            raise KeychainError('SDC_KEYCHAIN_UNAVAILABLE') from None

    def read(self, *, check=lambda: None):
        check()
        result = self._call('copy')
        if not isinstance(result, tuple) or len(result) != 2:
            raise KeychainError('SDC_KEYCHAIN_UNAVAILABLE')
        status, raw = result
        _status(status)
        return _blob(raw)

    def write(self, raw, *, check=lambda: None):
        raw = _blob(raw)
        check()
        status = self._call('add', raw)
        if status == _DUPLICATE:
            check()
            status = self._call('update', raw)
        _status(status)

    def delete(self, *, check=lambda: None):
        check()
        _status(self._call('delete'))


class _KeyCallbacks(ctypes.Structure):
    _fields_ = [('version', ctypes.c_long), ('retain', ctypes.c_void_p), ('release', ctypes.c_void_p),
                ('copyDescription', ctypes.c_void_p), ('equal', ctypes.c_void_p), ('hash', ctypes.c_void_p)]


class _ValueCallbacks(ctypes.Structure):
    _fields_ = _KeyCallbacks._fields_[:-1]


def _symbol(library, name):
    if name == 'kCFTypeDictionaryKeyCallBacks':
        return ctypes.addressof(_KeyCallbacks.in_dll(library, name))
    if name == 'kCFTypeDictionaryValueCallBacks':
        return ctypes.addressof(_ValueCallbacks.in_dll(library, name))
    return ctypes.c_void_p.in_dll(library, name).value


class _MacKeychain:
    """Bind fixed system frameworks lazily; injected libraries never touch macOS."""
    def __init__(self, *, libraries=None, symbol=_symbol):
        self._libraries, self._symbol, self._loaded = libraries, symbol, False

    def _load(self):
        if self._loaded:
            return
        if self._libraries is None:
            if sys.platform != 'darwin':
                raise KeychainError('SDC_KEYCHAIN_UNAVAILABLE')
            self._libraries = (ctypes.CDLL(CORE_FOUNDATION_FRAMEWORK), ctypes.CDLL(SECURITY_FRAMEWORK))
        self.cf, self.sec = self._libraries
        pointer, index = ctypes.c_void_p, ctypes.c_long
        bindings = {
            'CFStringCreateWithCString': ([pointer, ctypes.c_char_p, ctypes.c_uint32], pointer),
            'CFDataCreate': ([pointer, pointer, index], pointer),
            'CFDictionaryCreate': ([pointer, ctypes.POINTER(pointer), ctypes.POINTER(pointer), index, pointer, pointer], pointer),
            'CFRelease': ([pointer], None), 'CFGetTypeID': ([pointer], ctypes.c_ulong),
            'CFDataGetTypeID': ([], ctypes.c_ulong), 'CFDataGetLength': ([pointer], index),
            'CFDataGetBytePtr': ([pointer], pointer),
        }
        for name, (args, result) in bindings.items():
            function = getattr(self.cf, name)
            function.argtypes, function.restype = args, result
        bindings = {
            'SecItemCopyMatching': ([pointer, ctypes.POINTER(pointer)], ctypes.c_int32),
            'SecItemAdd': ([pointer, ctypes.POINTER(pointer)], ctypes.c_int32),
            'SecItemUpdate': ([pointer, pointer], ctypes.c_int32), 'SecItemDelete': ([pointer], ctypes.c_int32),
            'SecKeychainGetUserInteractionAllowed': ([ctypes.POINTER(ctypes.c_ubyte)], ctypes.c_int32),
            'SecKeychainSetUserInteractionAllowed': ([ctypes.c_ubyte], ctypes.c_int32),
        }
        for name, (args, result) in bindings.items():
            function = getattr(self.sec, name)
            function.argtypes, function.restype = args, result
        self.constants = {}
        for name in ('kSecClass', 'kSecClassGenericPassword', 'kSecAttrService', 'kSecAttrAccount',
                     'kSecValueData', 'kSecMatchLimit', 'kSecMatchLimitOne', 'kSecReturnData',
                     'kSecUseAuthenticationUI', 'kSecUseAuthenticationUIFail', 'kSecUseDataProtectionKeychain',
                     'kSecAttrSynchronizable'):
            self.constants[name] = self._symbol(self.sec, name)
        for name in ('kCFBooleanTrue', 'kCFBooleanFalse', 'kCFTypeDictionaryKeyCallBacks', 'kCFTypeDictionaryValueCallBacks'):
            self.constants[name] = self._symbol(self.cf, name)
        if not all(self.constants.values()):
            raise KeychainError('SDC_KEYCHAIN_UNAVAILABLE')
        self._loaded = True

    @contextmanager
    def _no_ui(self):
        self._load()
        with _UI_LOCK:
            previous = ctypes.c_ubyte()
            _status(self.sec.SecKeychainGetUserInteractionAllowed(ctypes.byref(previous)))
            try:
                _status(self.sec.SecKeychainSetUserInteractionAllowed(0))
                yield
            finally:
                # This process-local UI flag is not a Keychain unlock or ACL change.
                _status(self.sec.SecKeychainSetUserInteractionAllowed(previous.value))

    @contextmanager
    def _dictionary(self, pairs):
        owned, keys, values = [], [], []
        try:
            for key, value in pairs.items():
                keys.append(self.constants[key])
                if isinstance(value, tuple):
                    pointer = self.constants[value[0]]
                elif isinstance(value, bool):
                    pointer = self.constants['kCFBooleanTrue' if value else 'kCFBooleanFalse']
                elif isinstance(value, bytes):
                    buffer = ctypes.create_string_buffer(value)
                    try:
                        pointer = self.cf.CFDataCreate(None, ctypes.cast(buffer, ctypes.c_void_p), len(value))
                    finally:
                        ctypes.memset(buffer, 0, len(buffer))
                    owned.append(pointer)
                else:
                    pointer = self.cf.CFStringCreateWithCString(None, value.encode('utf-8'), 0x08000100)
                    owned.append(pointer)
                if not pointer:
                    raise KeychainError('SDC_KEYCHAIN_UNAVAILABLE')
                values.append(pointer)
            array = ctypes.c_void_p * len(keys)
            dictionary = self.cf.CFDictionaryCreate(None, array(*keys), array(*values), len(keys),
                                                    self.constants['kCFTypeDictionaryKeyCallBacks'], self.constants['kCFTypeDictionaryValueCallBacks'])
            if not dictionary:
                raise KeychainError('SDC_KEYCHAIN_UNAVAILABLE')
            owned.append(dictionary)
            yield dictionary
        finally:
            for pointer in reversed(owned):
                if pointer:
                    self.cf.CFRelease(pointer)

    def _query(self, account):
        return {'kSecClass': ('kSecClassGenericPassword',), 'kSecAttrService': SERVICE, 'kSecAttrAccount': account,
                'kSecUseAuthenticationUI': ('kSecUseAuthenticationUIFail',),
                'kSecUseDataProtectionKeychain': False, 'kSecAttrSynchronizable': False}

    def copy(self, account):
        with self._no_ui(), self._dictionary({**self._query(account), 'kSecMatchLimit': ('kSecMatchLimitOne',), 'kSecReturnData': True}) as query:
            result = ctypes.c_void_p()
            try:
                status = self.sec.SecItemCopyMatching(query, ctypes.byref(result))
                if status:
                    return status, None
                if not result.value or self.cf.CFGetTypeID(result) != self.cf.CFDataGetTypeID():
                    raise KeychainError('SDC_KEYCHAIN_INVALID_VALUE')
                length = self.cf.CFDataGetLength(result)
                if not 1 <= length <= MAX_BYTES:
                    raise KeychainError('SDC_KEYCHAIN_INVALID_VALUE')
                pointer = self.cf.CFDataGetBytePtr(result)
                if not pointer:
                    raise KeychainError('SDC_KEYCHAIN_INVALID_VALUE')
                return status, ctypes.string_at(pointer, length)
            finally:
                if result.value:
                    self.cf.CFRelease(result)

    def add(self, account, raw):
        with self._no_ui(), self._dictionary({**self._query(account), 'kSecValueData': raw}) as attributes:
            return self.sec.SecItemAdd(attributes, None)

    def update(self, account, raw):
        with self._no_ui(), self._dictionary(self._query(account)) as query, self._dictionary({'kSecValueData': raw}) as attributes:
            return self.sec.SecItemUpdate(query, attributes)

    def delete(self, account):
        with self._no_ui(), self._dictionary(self._query(account)) as query:
            return self.sec.SecItemDelete(query)
