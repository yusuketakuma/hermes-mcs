"""v3 stage deps面: 私有stage配置・sink/verify・bundle path・file digest。

stage配置は`<stage_root>/<artifact_sha256>/artifact.bundle`で決定論的 —
bundle_path(target)がprocess再起動後も同一pathを再導出できる必要がある
(applyはSTAGED後の別process/再起動で走り得る)。stage_sinkは呼出毎に
同pathをO_TRUNCで新規内容へ置き換える(sha256で内容は一意)。
"""
import hashlib
import os
import re
import stat

_CHUNK = 1024 * 1024
_BUNDLE_TIMEOUT_S = 30
_SHA256 = re.compile(r'[0-9a-f]{64}')


def file_digest(path):
    """{'sha256','size'}を返す。non-regular/読取不能はNone(fail-closed)。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(fd, _CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        return {'sha256': digest.hexdigest(), 'size': size}
    except OSError:
        return None
    finally:
        os.close(fd)


class Stage:
    """deps契約のstage_sink/stage_verify/bundle_path/file_digest面。

    stage_rootはfactoryが配備構成から注入する(control root配下の私有dir)。
    git_runは`git bundle verify`用のdeps.git_run互換callable
    (argv, env=, timeout_s=)->{'rc',...}。未注入ならstage_verifyは
    bundle構造を証明できず常にFalse(fail-closed)。
    """

    def __init__(self, *, stage_root, git_run=None):
        self._root = os.path.abspath(os.fspath(stage_root))
        self._git_run = git_run

    def _artifact_path(self, target):
        sha = target.get('artifact_sha256') if isinstance(target, dict) else None
        if type(sha) is not str or _SHA256.fullmatch(sha) is None:
            raise ValueError('STAGE_TARGET')
        return os.path.join(self._root, sha, 'artifact.bundle')

    def stage_sink(self, target):
        """私有stage fileを返す。呼出毎にO_TRUNCで新規内容へ置き換える。

        返すfile objectのclose責任は呼出側(fetch_artifact deps)にある。
        """
        path = self._artifact_path(target)
        directory = os.path.dirname(path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        info = os.lstat(directory)
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o700
                or info.st_uid != os.getuid()):
            raise ValueError('STAGE_DIR')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                     | os.O_NOFOLLOW, 0o600)
        return os.fdopen(fd, 'wb')

    def stage_verify(self, target, sink):
        """FD/path/inode/size/sha256とbundle構造を再照合する。"""
        try:
            path = self._artifact_path(target)
            if sink is None or not hasattr(sink, 'fileno'):
                return False
            file_stat = os.fstat(sink.fileno())
            try:
                path_stat = os.stat(path, follow_symlinks=False)
            except OSError:
                return False
            if ((file_stat.st_dev, file_stat.st_ino)
                    != (path_stat.st_dev, path_stat.st_ino)):
                return False
            if (not stat.S_ISREG(path_stat.st_mode)
                    or path_stat.st_uid != os.getuid()
                    or stat.S_IMODE(path_stat.st_mode) != 0o600
                    or path_stat.st_nlink != 1
                    or path_stat.st_size != target['artifact_size']):
                return False
            digest = file_digest(path)
            if (not isinstance(digest, dict)
                    or digest.get('sha256') != target['artifact_sha256']
                    or digest.get('size') != target['artifact_size']):
                return False
            if self._git_run is None:
                return False
            result = self._git_run(['bundle', 'verify', path],
                                   timeout_s=_BUNDLE_TIMEOUT_S)
            return isinstance(result, dict) and result.get('rc') == 0
        except (OSError, ValueError, TypeError):
            return False

    def bundle_path(self, target):
        return self._artifact_path(target)

    def file_digest(self, path):
        return file_digest(path)
