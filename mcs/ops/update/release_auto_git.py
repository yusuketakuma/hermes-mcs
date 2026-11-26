"""v3 apply depsのgit面: 制限環境run・repo監査・path解決。

release_auto_applyが必要とするgit観測面の実装。全commandは/usr/bin/gitへ
-c hardening(fsmonitor/hooksPath/untrackedCache/attributesFile/protocol)
を強制し、caller提供envの下に基底sanitized envを常に敷く。監査不能は
findings全項目Trueでfail-closed(unsafe扱い)を返す。
"""
import os
import subprocess

GIT_BIN = '/usr/bin/git'
_GIT_BASE_ENV = {'PATH': '/usr/bin:/bin', 'GIT_CONFIG_NOSYSTEM': '1',
                 'GIT_CONFIG_SYSTEM': '/dev/null', 'GIT_CONFIG_GLOBAL': '/dev/null',
                 'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': '/bin/false',
                 'GIT_SSH_COMMAND': '/bin/false', 'GIT_EDITOR': '/bin/false',
                 'GIT_PAGER': 'cat', 'PAGER': 'cat'}
_HARDENING = (('core.fsmonitor', 'false'), ('core.hooksPath', '/dev/null'),
              ('core.untrackedCache', 'false'),
              ('core.attributesFile', '/dev/null'), ('protocol.allow', 'never'))
_INSPECT_KEYS = ('alternates', 'replace_refs', 'includes', 'hooks_path',
                 'fsmonitor', 'external_filter')


class Git:
    """deps契約のrepository_path/git_run/git_inspect面。

    repository_path未指定時はmcs_util.REPO(配備checkout)を遅延解決する。
    """

    def __init__(self, repository_path=None, *, git_bin=GIT_BIN):
        self._repository = (os.fspath(repository_path)
                            if repository_path is not None else None)
        self._git_bin = git_bin

    def repository_path(self):
        if self._repository is None:
            import mcs_util
            self._repository = mcs_util.REPO
        return self._repository

    def run(self, argv, *, env=None, timeout_s=None):
        """argvはcallerの-C repo等を含む完全形。戻り値は{'rc','stdout','stderr'}。"""
        merged = dict(_GIT_BASE_ENV)
        if env:
            merged.update(env)
        cmd = [self._git_bin]
        for key, value in _HARDENING:
            cmd += ['-c', f'{key}={value}']
        cmd += [str(arg) for arg in argv]
        try:
            proc = subprocess.run(cmd, env=merged, capture_output=True,
                                  timeout=timeout_s, check=False)
        except subprocess.TimeoutExpired:
            return {'rc': 124, 'stdout': '', 'stderr': '', 'error': 'TIMEOUT'}
        except OSError:
            return {'rc': 126, 'stdout': '', 'stderr': '', 'error': 'SPAWN_FAILED'}
        return {'rc': proc.returncode,
                'stdout': proc.stdout.decode('utf-8', 'replace'),
                'stderr': proc.stderr.decode('utf-8', 'replace')}

    def git_inspect(self, repo):
        """repo local config/objectsのunsafe監査。判定不能は全項目True。"""
        unsafe = {key: True for key in _INSPECT_KEYS}
        try:
            cfg = self.run(['-C', repo, 'config', '--local', '--null', '--list'],
                           timeout_s=10)
            if cfg.get('rc') != 0:
                return unsafe
            includes = hooks = fsmon = extfilter = False
            for entry in cfg.get('stdout', '').split('\0'):
                if not entry:
                    continue
                key, _, value = entry.partition('\n')
                key = key.strip().lower()
                if key.startswith(('include.', 'includeif.')):
                    includes = True
                elif key == 'core.hookspath':
                    hooks = True
                elif (key == 'core.fsmonitor'
                      and value.strip().lower() not in ('', 'false', '0')):
                    fsmon = True
                elif (key.startswith('filter.')
                      and key.rsplit('.', 1)[-1] in ('clean', 'smudge', 'process')):
                    extfilter = True
            common = self.run(['-C', repo, 'rev-parse', '--git-common-dir'],
                              timeout_s=10)
            alternates = False
            if common.get('rc') == 0:
                gitdir = common['stdout'].strip()
                if gitdir and not os.path.isabs(gitdir):
                    gitdir = os.path.join(repo, gitdir)
                alternates = bool(gitdir) and os.path.lexists(
                    os.path.join(gitdir, 'objects', 'info', 'alternates'))
            replace = self.run(['-C', repo, 'replace', '-l'], timeout_s=10)
            return {'alternates': alternates,
                    'replace_refs': bool(replace.get('rc') == 0
                                         and replace.get('stdout', '').strip()),
                    'includes': includes, 'hooks_path': hooks,
                    'fsmonitor': fsmon, 'external_filter': extfilter}
        except (OSError, ValueError, TypeError):
            return unsafe
