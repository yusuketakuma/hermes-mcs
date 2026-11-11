"""v3 owner evidence store: deps.evidence_store/evidence_load の実装。

spec 02: 既存control root(`runtime_entry.control_root`)配下の`evidence/`へ
O_EXCL耐久保存する。0600 regular single-link・nofollow・file/dir fsync維持。
object_keyは内容sha256 — 同bytesは常に同keyで冪等、異bytesは別key。
symlink・非regular・hash不一致・サイズ超過は全て拒否する(fail-closed)。
"""
import hashlib
import os
import stat

from safe_io import create_exclusive

# spec 03: StoredEvidence objectは最大8MiB/file。
MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
_KEY_HEX = 64


class EvidenceError(RuntimeError):
    def __init__(self, code='EVIDENCE_HOLD'):
        super().__init__(code)
        self.code = code


def _check(ok, code='EVIDENCE_HOLD'):
    if not ok:
        raise EvidenceError(code)


def _control_dir(runtime_home):
    """control rootをdir_fdで開く。private末端2要素は0700/owner検証、mkdirも末端のみ。"""
    from runtime_entry import control_root
    root = control_root(runtime_home)
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = root.parts[1:]
        for index, part in enumerate(parts):
            private = index >= len(root.parts) - 3
            if private:
                try:
                    os.mkdir(part, 0o700, dir_fd=directory)
                    os.fsync(directory)
                except FileExistsError:
                    pass
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory)
            except OSError as e:
                raise EvidenceError('CONTROL_ROOT_HOLD') from e
            os.close(directory)
            directory = child
            info = os.fstat(directory)
            if private and (info.st_uid != os.getuid()
                            or stat.S_IMODE(info.st_mode) != 0o700):
                raise EvidenceError('CONTROL_ROOT_HOLD')
        return directory
    except BaseException:
        os.close(directory)
        raise


def _evidence_dir_fd(runtime_home):
    """control root/evidence/ をdir_fdで開く(無ければ0700で作成)。"""
    directory = _control_dir(runtime_home)
    try:
        try:
            os.mkdir('evidence', 0o700, dir_fd=directory)
            os.fsync(directory)
        except FileExistsError:
            pass
        try:
            child = os.open('evidence', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                             dir_fd=directory)
        except OSError as e:
            raise EvidenceError('EVIDENCE_DIR_HOLD') from e
        try:
            info = os.fstat(child)
            _check(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                   and stat.S_IMODE(info.st_mode) == 0o700, 'EVIDENCE_DIR_HOLD')
        except BaseException:
            os.close(child)
            raise
        return child
    finally:
        os.close(directory)


def store_evidence(runtime_home, raw):
    """raw bytesをcontrol root evidence/へO_EXCL耐久保存しobject_keyを返す。

    object_key=sha256 hex。同名既存は内容一致時のみ冪等受理、
    不一致(改竄/衝突)は拒否。書込みはfile+dir fsyncまで完了してから返す。
    """
    _check(type(raw) in (bytes, bytearray) and 0 < len(raw) <= MAX_EVIDENCE_BYTES,
           'EVIDENCE_SIZE')
    raw = bytes(raw)
    key = hashlib.sha256(raw).hexdigest()
    fd_dir = _evidence_dir_fd(runtime_home)
    try:
        try:
            fd = create_exclusive(key, 0o600, dir_fd=fd_dir)
        except FileExistsError:
            existing = load_evidence(runtime_home, key)
            _check(existing == raw, 'EVIDENCE_CONFLICT')
            return key
        except OSError as e:
            raise EvidenceError('EVIDENCE_HOLD') from e
        try:
            view = memoryview(raw)
            while view:
                written = os.write(fd, view)
                _check(written > 0, 'EVIDENCE_WRITE')
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(fd_dir)
        return key
    finally:
        os.close(fd_dir)


def load_evidence(runtime_home, object_key):
    """保存済み証拠を読戻す。key形式・regular・nofollow・サイズ上限を検査。"""
    _check(type(object_key) is str and len(object_key) == _KEY_HEX
           and all(c in '0123456789abcdef' for c in object_key),
           'EVIDENCE_KEY')
    fd_dir = _evidence_dir_fd(runtime_home)
    try:
        try:
            fd = os.open(object_key,
                         os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=fd_dir)
        except FileNotFoundError:
            raise EvidenceError('EVIDENCE_MISSING') from None
        except OSError as e:
            raise EvidenceError('EVIDENCE_HOLD') from e
        try:
            info = os.fstat(fd)
            _check(stat.S_ISREG(info.st_mode)
                   and info.st_uid == os.getuid()
                   and info.st_nlink == 1
                   and stat.S_IMODE(info.st_mode) == 0o600
                   and 0 < info.st_size <= MAX_EVIDENCE_BYTES,
                   'EVIDENCE_HOLD')
            chunks, total = [], 0
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                total += len(chunk)
                _check(total <= MAX_EVIDENCE_BYTES, 'EVIDENCE_HOLD')
                chunks.append(chunk)
            raw = b''.join(chunks)
            _check(len(raw) == info.st_size
                   and hashlib.sha256(raw).hexdigest() == object_key,
                   'EVIDENCE_TAMPER')
            return raw
        finally:
            os.close(fd)
    finally:
        os.close(fd_dir)
