"""Exercise only fake providers and fake framework functions, never Keychain."""
import ctypes

import pytest

from adapters.sdc import keychain
from adapters.sdc.keychain import KeychainCredentialStore, KeychainError, SERVICE

ACCOUNT = '11111111-1111-4111-8111-111111111111'
OTHER_ACCOUNT = '22222222-2222-4222-8222-222222222222'
FIRST = b'{"key":"SYNTHETIC_FIRST_SECRET"}'
SECOND = b'{"key":"SYNTHETIC_SECOND_SECRET"}'


class FakeNative:
    def __init__(self):
        self.items, self.calls, self.errors = {}, [], {}

    def copy(self, account):
        self.calls.append(('copy', account))
        return self.errors.get('copy', 0 if (SERVICE, account) in self.items else -25300), self.items.get((SERVICE, account))

    def add(self, account, raw):
        self.calls.append(('add', account))
        status = self.errors.get('add', -25299 if (SERVICE, account) in self.items else 0)
        if status == 0:
            self.items[SERVICE, account] = raw
        return status

    def update(self, account, raw):
        self.calls.append(('update', account))
        status = self.errors.get('update', 0 if (SERVICE, account) in self.items else -25300)
        if status == 0:
            self.items[SERVICE, account] = raw
        return status

    def delete(self, account):
        self.calls.append(('delete', account))
        status = self.errors.get('delete', 0 if (SERVICE, account) in self.items else -25300)
        if status == 0:
            del self.items[SERVICE, account]
        return status


def test_new_item_update_and_delete_are_limited_to_the_same_service_and_account():
    native = FakeNative()
    native.items['unrelated.service', ACCOUNT] = FIRST
    native.items[SERVICE, OTHER_ACCOUNT] = FIRST
    store = KeychainCredentialStore(ACCOUNT, native=native)
    store.write(FIRST)
    assert store.read() == FIRST
    store.write(SECOND)
    assert store.read() == SECOND
    assert native.calls == [('add', ACCOUNT), ('copy', ACCOUNT), ('add', ACCOUNT), ('update', ACCOUNT), ('copy', ACCOUNT)]
    store.delete()
    assert native.items == {('unrelated.service', ACCOUNT): FIRST, (SERVICE, OTHER_ACCOUNT): FIRST}
    with pytest.raises(KeychainError, match='^SDC_KEYCHAIN_NOT_FOUND$'):
        store.read()


@pytest.mark.parametrize('status,code', [(-25300, 'NOT_FOUND'), (-25308, 'LOCKED'), (-25315, 'LOCKED'),
                                       (-25293, 'AUTH_FAILED'), (-128, 'AUTH_FAILED'), (-25291, 'UNAVAILABLE'),
                                       (-34018, 'UNAVAILABLE'), (-99999, 'UNAVAILABLE')])
def test_native_statuses_are_fixed_and_never_trigger_plaintext_fallback(status, code, capsys):
    native = FakeNative()
    native.errors['copy'] = status
    store = KeychainCredentialStore(ACCOUNT, native=native)
    with pytest.raises(KeychainError) as error:
        store.read()
    assert error.value.code == 'SDC_KEYCHAIN_' + code
    assert str(error.value) == error.value.code
    assert isinstance(error.value, ValueError)
    assert native.calls == [('copy', ACCOUNT)]
    assert capsys.readouterr() == ('', '')


def test_locked_update_preserves_existing_item_and_does_not_delete_or_readd():
    native = FakeNative()
    native.items[SERVICE, ACCOUNT] = FIRST
    native.errors['update'] = -25308
    with pytest.raises(KeychainError, match='^SDC_KEYCHAIN_LOCKED$'):
        KeychainCredentialStore(ACCOUNT, native=native).write(SECOND)
    assert native.items[SERVICE, ACCOUNT] == FIRST
    assert native.calls == [('add', ACCOUNT), ('update', ACCOUNT)]


@pytest.mark.parametrize('account', ['mcs-adapter', 'SYNTHETIC_PRIVATE_ACCOUNT', '../credential', None,
                                    '11111111-1111-1111-8111-111111111111', ACCOUNT.upper().replace('1', 'A')])
def test_only_canonical_local_uuid4_accounts_are_accepted(account):
    native = FakeNative()
    with pytest.raises(KeychainError, match='^SDC_KEYCHAIN_INVALID_ACCOUNT$'):
        KeychainCredentialStore(account, native=native)
    assert native.calls == []


@pytest.mark.parametrize('raw', [b'', b'x' * 4097, b'\xff', b'{"secret":"SYNTHETIC_SECRET",',
                                b'{"a":1,"a":2}', b'{"a":NaN}', b'[[[[[[[[[[0]]]]]]]]]]', 'SYNTHETIC_SECRET'])
def test_invalid_or_oversized_json_never_reaches_native_or_error_output(raw):
    native = FakeNative()
    with pytest.raises(KeychainError) as error:
        KeychainCredentialStore(ACCOUNT, native=native).write(raw)
    assert error.value.code == 'SDC_KEYCHAIN_INVALID_VALUE'
    assert 'SYNTHETIC_SECRET' not in str(error.value)
    assert native.calls == []


def test_four_kib_boundary_and_caller_owned_schema_validation():
    native = FakeNative()
    store = KeychainCredentialStore(ACCOUNT, native=native)
    raw = b'{"s":"' + b'x' * 4088 + b'"}'
    assert len(raw) == 4096
    store.write(raw)
    assert store.read() == raw
    store.write(b'null')
    assert store.read() == b'null'


def test_invalid_native_data_is_rejected_without_exposing_content():
    native = FakeNative()
    native.items[SERVICE, ACCOUNT] = b'SYNTHETIC_SECRET'
    with pytest.raises(KeychainError, match='^SDC_KEYCHAIN_INVALID_VALUE$'):
        KeychainCredentialStore(ACCOUNT, native=native).read()


def test_construction_does_not_load_frameworks_and_non_mac_operations_fail_closed(monkeypatch):
    monkeypatch.setattr(keychain.ctypes, 'CDLL', lambda *_: pytest.fail('no framework may load'))
    store = KeychainCredentialStore(ACCOUNT)
    assert store.native is None
    monkeypatch.setattr(keychain.sys, 'platform', 'linux')
    with pytest.raises(KeychainError, match='^SDC_KEYCHAIN_UNAVAILABLE$'):
        store.read()


def test_native_loader_failure_is_fixed_and_never_uses_security_subprocess(monkeypatch, capsys):
    calls = []
    def unavailable(path):
        calls.append(path)
        raise OSError('SYNTHETIC_SECRET')
    monkeypatch.setattr(keychain.sys, 'platform', 'darwin')
    monkeypatch.setattr(keychain.ctypes, 'CDLL', unavailable)
    with pytest.raises(KeychainError, match='^SDC_KEYCHAIN_UNAVAILABLE$'):
        KeychainCredentialStore(ACCOUNT).read()
    assert calls == [keychain.CORE_FOUNDATION_FRAMEWORK]
    assert capsys.readouterr() == ('', '')


def test_native_exception_text_is_not_propagated_but_deadlines_are_preserved():
    class Broken:
        def copy(self, _):
            raise RuntimeError('SYNTHETIC_SECRET')
    with pytest.raises(KeychainError, match='^SDC_KEYCHAIN_UNAVAILABLE$'):
        KeychainCredentialStore(ACCOUNT, native=Broken()).read()
    class Expired:
        def copy(self, _):
            raise TimeoutError('synthetic caller deadline')
    with pytest.raises(TimeoutError):
        KeychainCredentialStore(ACCOUNT, native=Expired()).read()


@pytest.fixture
def current_store(tmp_path):
    from adapters.sdc.storage import Store
    with Store(tmp_path / 'private', create=True,
               keychain_factory=lambda *_: pytest.fail('owned reader must not call native')) as store:
        store.write('endpoint.json', {'origin': 'https://synthetic.invalid',
                                     'audience': 'sdc:synthetic', 'environment': 'synthetic'})
        store.enable_customer()
        yield store


def test_owned_reader_uses_fresh_bytes_and_keeps_store_factory(current_store):
    copies = []
    raw = b'{"key":"' + b'a1' * 32 + b'"}'
    def copy_current(account):
        copies.append(account)
        return raw
    factory = current_store._keychain_factory
    assert current_store.read_current_credential(copy_current=copy_current)['key'] == 'a1' * 32
    raw = b'{"key":"' + b'b2' * 32 + b'"}'
    assert current_store.read_current_credential(copy_current=copy_current)['key'] == 'b2' * 32
    assert len(copies) == 2 and copies[0] == copies[1]
    assert current_store._keychain_factory is factory and current_store._keychain_store is None


@pytest.mark.parametrize('change', ['account', 'endpoint', 'root'])
def test_owned_reply_rechecks_root_endpoint_and_account(current_store, change):
    def copy_current(account):
        if change == 'account':
            marker = current_store.read('credential-store.json')
            marker['account'] = OTHER_ACCOUNT
            current_store._write_file('credential-store.json', marker)
        elif change == 'endpoint':
            current_store._write_file('endpoint.json', {'origin': 'https://other.invalid',
                'audience': 'sdc:synthetic', 'environment': 'synthetic'})
        else:
            current_store.root.rename(current_store.root.with_name('old-private'))
            current_store.root.mkdir(mode=0o700)
        return b'{"key":"' + b'a1' * 32 + b'"}'
    with pytest.raises(ValueError):
        current_store.read_current_credential(copy_current=copy_current)


@pytest.mark.parametrize('error', ['NOT_FOUND', 'LOCKED'])
def test_owned_keychain_failure_never_falls_back_to_plaintext(current_store, error):
    current_store._write_file('credential', {'key': 'a1' * 32})
    def missing(_):
        raise KeychainError('SDC_KEYCHAIN_' + error)
    if error == 'NOT_FOUND':
        assert current_store.read_current_credential(copy_current=missing) is None
    else:
        with pytest.raises(KeychainError, match='^SDC_KEYCHAIN_LOCKED$'):
            current_store.read_current_credential(copy_current=missing)


class Function:
    def __init__(self, call):
        self.call = call

    def __call__(self, *args):
        return self.call(*args)


def pointer(value):
    return value.value if isinstance(value, ctypes.c_void_p) else value


class FakeFrameworks:
    """Model CF ownership and Security calls without loading any real library."""
    def __init__(self):
        self.objects, self.constants, self.released, self.buffers = {}, {}, [], []
        self.items, self.queries, self.ui_changes = {}, [], []
        self.ui, self.fail_restore, self.fail_disable, self.get_error, self.operation_error, self.wrong_type = 1, False, False, 0, 0, False
        self.cf, self.sec = type('CF', (), {})(), type('Security', (), {})()
        methods = {
            'CFStringCreateWithCString': lambda _, raw, encoding: self.new(raw.decode('utf-8')),
            'CFDataCreate': lambda _, raw, size: self.new(ctypes.string_at(raw, size)),
            'CFDictionaryCreate': lambda _, keys, values, count, kc, vc: self.new({self.objects[keys[i]]: self.objects[values[i]] for i in range(count)}),
            'CFRelease': lambda value: self.released.append(pointer(value)),
            'CFGetTypeID': lambda _: 99 if self.wrong_type else 7,
            'CFDataGetTypeID': lambda: 7,
            'CFDataGetLength': lambda value: len(self.objects[pointer(value)]),
            'CFDataGetBytePtr': self.data_pointer,
        }
        for name, function in methods.items():
            setattr(self.cf, name, Function(function))
        methods = {'SecKeychainGetUserInteractionAllowed': self.get_ui, 'SecKeychainSetUserInteractionAllowed': self.set_ui,
                   'SecItemAdd': self.add, 'SecItemCopyMatching': self.copy, 'SecItemUpdate': self.update, 'SecItemDelete': self.delete}
        for name, function in methods.items():
            setattr(self.sec, name, Function(function))

    def new(self, value):
        handle = len(self.objects) + 1
        self.objects[handle] = value
        return handle

    def symbol(self, _, name):
        if name not in self.constants:
            value = True if name == 'kCFBooleanTrue' else False if name == 'kCFBooleanFalse' else name
            self.constants[name] = self.new(value)
        return self.constants[name]

    def data_pointer(self, value):
        buffer = ctypes.create_string_buffer(self.objects[pointer(value)])
        self.buffers.append(buffer)
        return ctypes.addressof(buffer)

    def get_ui(self, result):
        if self.get_error:
            return self.get_error
        ctypes.cast(result, ctypes.POINTER(ctypes.c_ubyte))[0] = self.ui
        return 0

    def set_ui(self, value):
        self.ui_changes.append(value)
        if value == 0 and self.fail_disable:
            return -25291
        if value == 1 and self.fail_restore:
            return -25291
        self.ui = value
        return 0

    def query(self, handle):
        assert self.ui == 0
        query = self.objects[pointer(handle)]
        self.queries.append(dict(query))
        assert query['kSecClass'] == 'kSecClassGenericPassword'
        assert query['kSecAttrService'] == SERVICE
        assert query['kSecAttrAccount'] == ACCOUNT
        assert query['kSecUseAuthenticationUI'] == 'kSecUseAuthenticationUIFail'
        assert query['kSecUseDataProtectionKeychain'] is False
        assert query['kSecAttrSynchronizable'] is False
        return query

    def add(self, handle, _):
        query = self.query(handle)
        if self.operation_error:
            return self.operation_error
        account = query['kSecAttrAccount']
        if account in self.items:
            return -25299
        self.items[account] = query['kSecValueData']
        return 0

    def copy(self, handle, result):
        query = self.query(handle)
        assert query['kSecMatchLimit'] == 'kSecMatchLimitOne'
        assert query['kSecReturnData'] is True
        if self.operation_error:
            return self.operation_error
        if query['kSecAttrAccount'] not in self.items:
            return -25300
        ctypes.cast(result, ctypes.POINTER(ctypes.c_void_p))[0] = self.new(self.items[query['kSecAttrAccount']])
        return 0

    def update(self, query_handle, attributes):
        query = self.query(query_handle)
        if self.operation_error:
            return self.operation_error
        changes = self.objects[pointer(attributes)]
        assert set(changes) == {'kSecValueData'}
        self.items[query['kSecAttrAccount']] = changes['kSecValueData']
        return 0

    def delete(self, handle):
        query = self.query(handle)
        if self.operation_error:
            return self.operation_error
        if query['kSecAttrAccount'] not in self.items:
            return -25300
        del self.items[query['kSecAttrAccount']]
        return 0

    def native(self):
        return keychain._MacKeychain(libraries=(self.cf, self.sec), symbol=self.symbol)


def test_native_cf_bridge_scopes_queries_uses_no_ui_and_releases_all_owned_refs():
    frameworks = FakeFrameworks()
    store = KeychainCredentialStore(ACCOUNT, native=frameworks.native())
    assert frameworks.constants == {}
    store.write(FIRST)
    assert store.read() == FIRST
    store.write(SECOND)
    assert store.read() == SECOND
    store.delete()
    assert frameworks.ui == 1
    assert frameworks.ui_changes == [0, 1] * 6
    constants = set(frameworks.constants.values())
    assert set(frameworks.released) == set(frameworks.objects) - constants
    assert len(frameworks.released) == len(set(frameworks.released))
    assert frameworks.sec.SecItemCopyMatching.restype is ctypes.c_int32
    assert frameworks.cf.CFDataGetLength.restype is ctypes.c_long


def test_native_failures_and_invalid_data_restore_the_original_ui_state():
    frameworks = FakeFrameworks()
    frameworks.operation_error = -25308
    store = KeychainCredentialStore(ACCOUNT, native=frameworks.native())
    with pytest.raises(KeychainError, match='^SDC_KEYCHAIN_LOCKED$'):
        store.read()
    assert frameworks.ui == 1 and frameworks.ui_changes == [0, 1]
    frameworks.operation_error = 0
    frameworks.items[ACCOUNT] = FIRST
    frameworks.wrong_type = True
    with pytest.raises(KeychainError, match='^SDC_KEYCHAIN_INVALID_VALUE$'):
        store.read()
    assert frameworks.ui == 1
    assert set(frameworks.released) == set(frameworks.objects) - set(frameworks.constants.values())


def test_preexisting_no_ui_mode_is_never_changed_to_allow():
    frameworks = FakeFrameworks()
    frameworks.ui = 0
    KeychainCredentialStore(ACCOUNT, native=frameworks.native()).write(FIRST)
    assert frameworks.ui == 0
    assert frameworks.ui_changes == [0, 0]


def test_ui_restoration_failure_is_unavailable_instead_of_success():
    frameworks = FakeFrameworks()
    frameworks.fail_restore = True
    with pytest.raises(KeychainError, match='^SDC_KEYCHAIN_UNAVAILABLE$'):
        KeychainCredentialStore(ACCOUNT, native=frameworks.native()).write(FIRST)
    assert frameworks.items[ACCOUNT] == FIRST


@pytest.mark.parametrize('failure', ['get', 'disable'])
def test_missing_no_ui_guarantee_prevents_every_item_operation(failure):
    frameworks = FakeFrameworks()
    frameworks.get_error = -25291 if failure == 'get' else 0
    frameworks.fail_disable = failure == 'disable'
    with pytest.raises(KeychainError, match='^SDC_KEYCHAIN_UNAVAILABLE$'):
        KeychainCredentialStore(ACCOUNT, native=frameworks.native()).write(FIRST)
    assert frameworks.queries == []
    assert frameworks.items == {}
    assert frameworks.ui == 1


def test_native_oversized_blob_is_rejected_before_copying_its_bytes(monkeypatch):
    frameworks = FakeFrameworks()
    frameworks.items[ACCOUNT] = b'x' * 4097
    native = frameworks.native()
    frameworks.cf.CFDataGetBytePtr = Function(lambda _: pytest.fail('oversized data must not be copied'))
    with pytest.raises(KeychainError, match='^SDC_KEYCHAIN_INVALID_VALUE$'):
        KeychainCredentialStore(ACCOUNT, native=native).read()
    assert frameworks.ui == 1
