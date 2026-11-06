"""Seal bounded read observations for patient readiness revalidation."""
import hashlib
import re
import sqlite3
import struct

MAX_QUERIES = 8192
MAX_ROWS = 100000
MAX_HANDLES = 200000
MAX_BYTES = 16 * 1024 * 1024
MAX_VALUE_BYTES = 2 * 1024 * 1024

_FUNCTIONS = frozenset({
    'abs', 'avg', 'char', 'coalesce', 'count', 'glob', 'group_concat', 'hex', 'ifnull',
    'iif', 'instr', 'json', 'json_array', 'json_array_length', 'json_extract',
    'json_insert', 'json_object', 'json_patch', 'json_quote', 'json_remove',
    'json_replace', 'json_set', 'json_type', 'json_valid', 'length', 'like', 'lower',
    'ltrim', 'max', 'min', 'nullif', 'printf', 'replace', 'round', 'rtrim',
    'substr', 'substring', 'sum', 'total', 'trim', 'typeof', 'unicode', 'upper', 'octet_length',
    'mcs_thin_fact_count',
})
_PRAGMAS = frozenset({'data_version', 'table_info', 'table_xinfo', 'table_list',
                      'index_list', 'index_info', 'index_xinfo', 'encoding', 'database_list'})
_PRAGMA_NO_ARGUMENT = frozenset({'data_version', 'encoding', 'database_list'})
_DATA_VERSION = re.compile(r'^\s*PRAGMA\s+(?:main\.)?data_version\s*;?\s*$', re.I)
_READ = re.compile(r'^\s*(?:SELECT|WITH|PRAGMA)\b', re.I)
_SAVEPOINT = re.compile(r'^\s*(?:SAVEPOINT|RELEASE(?:\s+SAVEPOINT)?|ROLLBACK\s+TO(?:\s+SAVEPOINT)?)\s+[A-Za-z_][A-Za-z_0-9]*\s*;?\s*$', re.I)


def _value(value):
    if value is None:
        return b'n'
    if type(value) is bool:
        return b'b1' if value else b'b0'
    if type(value) is int:
        if not -(2**63) <= value < 2**63:
            raise ValueError('witness_value')
        return b'i' + str(value).encode('ascii')
    if type(value) is float:
        return b'f' + struct.pack('!d', value)
    if type(value) is str:
        if len(value) * 4 > MAX_VALUE_BYTES:
            raise ValueError('witness_value')
        raw = value.encode('utf-8')
        return b's' + raw
    if type(value) in (bytes, bytearray, memoryview):
        if len(value) > MAX_VALUE_BYTES:
            raise ValueError('witness_value')
        return b'x' + bytes(value)
    raise ValueError('witness_value')


def _values(values):
    out = bytearray()
    for value in values:
        part = _value(value)
        if len(out) + len(part) + 8 > MAX_VALUE_BYTES:
            raise ValueError('witness_value')
        out.extend(len(part).to_bytes(8, 'big'))
        out.extend(part)
    return bytes(out)


def _parameters(params):
    if type(params) in (tuple, list):
        values = tuple(bytes(v) if type(v) in (bytearray, memoryview) else v for v in params)
        return values, b'p' + _values(values)
    if type(params) is dict and all(type(key) is str for key in params):
        pairs = sorted((key, bytes(value) if type(value) in (bytearray, memoryview) else value)
                       for key, value in params.items())
        return dict(pairs), b'm' + _values(item for pair in pairs for item in pair)
    raise ValueError('witness_params')


def freeze_parameters(params):
    """Copy SQLite bind values before execution or reject unsupported adapters."""
    try:
        return _parameters(params)[0]
    except (TypeError, UnicodeError, OverflowError, MemoryError) as error:
        raise ValueError('witness_params') from error


def _columns(description):
    if (not isinstance(description, (tuple, list))
            or any(not isinstance(column, (tuple, list)) or not column for column in description)):
        raise ValueError('witness_columns')
    names = tuple(column[0] for column in description)
    if any(type(name) is not str for name in names):
        raise ValueError('witness_columns')
    return names


def _row_digest(row, drug_progress):
    encoded = _values(tuple(row))  # Also enforce the original raw-value size limit.
    if drug_progress:
        import drug_map
        progress = drug_map.parse_annotation_progress(row[0])
        if not progress.get('invalid'):
            generation = progress['dictionary']
            values = ('valid_drug_generation', None) if generation is None else (
                'valid_drug_generation', *generation)
            encoded = _values(values)
    return hashlib.sha256(encoded).digest()


class _Handle:
    def __init__(self, owner, entry):
        self._owner, self._entry = owner, entry
        self._position, self._closed = 0, False
        self._eof = False

    def row(self, row):
        if self._closed or not self._owner._active:
            self._owner.fail()
            return
        if not self._owner._valid:
            return
        try:
            if not isinstance(row, sqlite3.Row):
                raise ValueError('witness_row')
            entry, pos = self._entry, self._position
            digest = _row_digest(row, entry['drug_progress'])
            if entry['eof'] is not None and pos >= entry['eof']:
                raise ValueError('witness_inconsistent')
            if pos < len(entry['rows']):
                if entry['rows'][pos] != digest:
                    raise ValueError('witness_inconsistent')
            else:
                self._owner._rows += 1
                self._owner._bytes += len(digest)
                if self._owner._rows > MAX_ROWS or self._owner._bytes > MAX_BYTES:
                    raise ValueError('witness_limit')
                entry['rows'].append(digest)
            self._position += 1
        except (ValueError, TypeError, UnicodeError, OverflowError, MemoryError):
            self._owner.fail()

    def eof(self):
        if self._closed:
            if not self._eof:
                self._owner.fail()
            return
        if (self._position < len(self._entry['rows'])
                or self._entry['eof'] is not None and self._entry['eof'] != self._position):
            self._owner.fail()
        self._entry['eof'] = self._position
        self._eof = True
        self.close()

    def close(self):
        if not self._closed:
            self._closed = True
            self._owner._open -= 1


class _IgnoredHandle:
    def row(self, row):
        pass

    def eof(self):
        pass

    def close(self):
        pass


class Recording:
    """Record only deterministic reads on an owned, already active snapshot."""
    def __init__(self, db, *, drug_generation_only=False):
        self._db = db
        # Only use this when the entire observed closure ignores the cursor.
        self._drug_generation_only = drug_generation_only is True
        self._entries = {}
        self._active = self._valid = False
        self._open = self._rows = self._bytes = self._handles = 0
        self._before = None
        self._authorizer_epoch = None
        self._sealed = False
        self._guard = {'valid': True, 'closed': False}

    @property
    def drug_progress_observed(self):
        return any(entry['drug_progress'] for entry in self._entries.values())

    def _authorize(self, action, first, second, database, source):
        if self._sealed:
            self.fail()
        allowed = action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_RECURSIVE)
        if action == sqlite3.SQLITE_READ:
            allowed = database in (None, 'main', 'temp')
        elif action == sqlite3.SQLITE_FUNCTION:
            name = second.lower() if isinstance(second, str) else ''
            if name not in _FUNCTIONS:
                self.fail()
            allowed = bool(name) and name != 'load_extension'
        elif action == sqlite3.SQLITE_PRAGMA:
            name = first.lower() if isinstance(first, str) else ''
            allowed = name in _PRAGMAS and (name not in _PRAGMA_NO_ARGUMENT or second is None)
        elif action == sqlite3.SQLITE_SAVEPOINT:
            allowed = first in ('BEGIN', 'RELEASE', 'ROLLBACK')
        if not allowed:
            self.fail()
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def __enter__(self):
        try:
            if (self._active or not self._db.in_transaction or self._db.row_factory is not sqlite3.Row
                    or getattr(self._db, 'witness_authorizer_present', None) is not False
                    or type(getattr(self._db, 'witness_authorizer_epoch', None)) is not int
                    or getattr(self._db, 'read_witness', None) is not None):
                return self
            if self._db.execute('PRAGMA query_only').fetchone()[0] != 1:
                return self
            self._before = (self._db.execute('PRAGMA data_version').fetchone()[0], self._db.total_changes)
            self._authorizer_epoch = self._db.witness_authorizer_epoch
            sqlite3.Connection.set_authorizer(self._db, self._authorize)
            self._active = self._valid = True
            self._db.read_witness = self
        except Exception:
            self.fail()
        return self

    def __exit__(self, kind, value, traceback):
        if kind is not None:
            self.fail()
        if self._active:
            try:
                if getattr(self._db, 'read_witness', None) is self:
                    self._db.read_witness = None
                if (getattr(self._db, 'witness_authorizer_present', None) is False
                        and getattr(self._db, 'witness_authorizer_epoch', None) == self._authorizer_epoch):
                    sqlite3.Connection.set_authorizer(self._db, None)
                else:
                    self.fail()
            except sqlite3.Error:
                self.fail()
        self._active = False
        self._guard['closed'] = True
        return False

    def fail(self):
        self._valid = False
        self._guard['valid'] = False

    def start(self, sql, params, description):
        if not self._active or not self._valid or self._sealed or type(sql) is not str:
            self.fail()
            return _IgnoredHandle()
        if _DATA_VERSION.fullmatch(sql):
            return _IgnoredHandle()
        if description is None and _SAVEPOINT.fullmatch(sql):
            return _IgnoredHandle()
        try:
            if not _READ.match(sql) or len(sql.encode('utf-8')) > MAX_VALUE_BYTES:
                raise ValueError('witness_sql')
            values, encoded = _parameters(params)
            columns = _columns(description)
            key = (sql, encoded)
            entry = self._entries.get(key)
            if entry is None:
                self._bytes += len(sql.encode('utf-8')) + len(encoded) + len(_values(columns))
                if len(self._entries) >= MAX_QUERIES or self._bytes > MAX_BYTES:
                    raise ValueError('witness_limit')
                drug_progress = False
                if self._drug_generation_only:
                    import drug_map
                    drug_progress = (sql == drug_map.ANNOTATION_PROGRESS_SQL
                                     and values == (drug_map.PROGRESS_KIND,)
                                     and columns == ('content',))
                entry = {'sql': sql, 'params': values, 'columns': columns, 'rows': [],
                         'eof': None, 'drug_progress': drug_progress}
                self._entries[key] = entry
            elif entry['columns'] != columns:
                raise ValueError('witness_inconsistent')
            self._handles += 1
            if self._handles > MAX_HANDLES:
                raise ValueError('witness_limit')
            self._open += 1
            return _Handle(self, entry)
        except (ValueError, TypeError, UnicodeError, OverflowError, MemoryError):
            self.fail()
            return _IgnoredHandle()

    def seal(self):
        if not self._active or not self._valid or self._sealed or self._open or not self._entries:
            return None
        try:
            if (not self._db.in_transaction or self._db.row_factory is not sqlite3.Row
                    or getattr(self._db, 'read_witness', None) is not self
                    or getattr(self._db, 'witness_authorizer_present', None) is not False
                    or getattr(self._db, 'witness_authorizer_epoch', None) != self._authorizer_epoch
                    or (self._db.execute('PRAGMA data_version').fetchone()[0], self._db.total_changes) != self._before):
                return None
            entries = tuple((e['sql'], e['params'], e['columns'], tuple(e['rows']),
                             e['eof'] is not None, e['drug_progress'])
                            for e in self._entries.values())
            self._sealed = True  # a seal is a one-shot end to observation
            return Witness(entries, self._guard)
        except Exception:
            self.fail()
            return None


class Witness:
    """An in-memory observation; never includes clinical text in its representation."""
    def __init__(self, entries, guard):
        self._entries = entries
        self._guard = guard

    def matches(self, db, deadline, clock):
        try:
            if (not self._guard['valid'] or not self._guard['closed']
                    or not db.in_transaction or db.row_factory is not sqlite3.Row):
                return False
            before = db.total_changes
            for sql, params, columns, rows, eof, drug_progress in self._entries:
                if clock() >= deadline:
                    return False
                cursor = db.execute(sql, params)
                try:
                    if _columns(cursor.description) != columns:
                        return False
                    for expected in rows:
                        if clock() >= deadline:
                            return False
                        row = cursor.fetchone()
                        if (not isinstance(row, sqlite3.Row)
                                or _row_digest(row, drug_progress) != expected):
                            return False
                    if eof and (clock() >= deadline or cursor.fetchone() is not None):
                        return False
                finally:
                    cursor.close()
            return (self._guard['valid'] and db.in_transaction
                    and db.total_changes == before and clock() < deadline)
        except Exception:
            return False
