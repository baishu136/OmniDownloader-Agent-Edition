import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet

from .config import atomic_write, private_dir


def core_root():
    if getattr(sys, 'frozen', False): return Path(sys._MEIPASS) / 'windows'
    return Path(__file__).resolve().parents[2] / 'windows'


def native_runtime(name):
    import shutil
    roots = [core_root()]
    if getattr(sys,'frozen',False): roots.insert(0,Path(sys._MEIPASS))
    for root in roots:
        path=root/'bin'/(name+'.exe' if os.name=='nt' else name)
        if path.is_file(): return str(path)
    return shutil.which(name)


class Vault:
    def __init__(self, directory):
        self.directory = private_dir(Path(directory) / 'credentials')
        self.fernet = None
        if os.name != 'nt':
            key = self.directory / 'master.key'
            if not key.exists(): atomic_write(key, Fernet.generate_key())
            if key.stat().st_mode & 0o077: raise PermissionError('master.key 必须仅当前用户可读')
            self.fernet = Fernet(key.read_bytes())

    def protect(self, data, decrypt=False):
        if self.fernet:
            return self.fernet.decrypt(data) if decrypt else self.fernet.encrypt(data)
        return self.dpapi(data, decrypt)

    def bind_core(self):
        sys.path.insert(0, str(core_root()))
        # The legacy module constructs its default store at import time. Direct
        # that temporary instance into writable Agent state, even under a
        # read-only Docker root filesystem. Restore worker-specific state after.
        previous = os.environ.get('OMNI_STATE_DIR')
        if previous is None: os.environ['OMNI_STATE_DIR'] = str(self.directory)
        try:
            import core.session_store as module
        finally:
            if previous is None: os.environ.pop('OMNI_STATE_DIR',None)
        if not hasattr(module, '_agent_original_protect'):
            module._agent_original_protect = module.protect
        self.dpapi = module._agent_original_protect
        module.protect = self.protect
        module.sessions = module.SessionStore(self.directory)
        return module.sessions, module.DOMAINS

    def put(self, name, value):
        atomic_write(self.directory / (name + '.secret'), self.protect(value.encode()))

    def get(self, name):
        path = self.directory / (name + '.secret')
        return self.protect(path.read_bytes(), True).decode() if path.exists() else ''
