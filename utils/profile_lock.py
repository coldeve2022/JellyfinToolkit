"""A process-held OS lock for one profile. A leftover lock file is harmless."""
from pathlib import Path
import os
import json
from utils.data_profile import ProfileError


class ProfileLock:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self._stream = None

    def acquire(self):
        if self._stream is not None:
            return self
        if not self.root.is_dir():
            raise ProfileError('资料库目录不存在：' + str(self.root))
        stream = (self.root / '.profile.lock').open('a+b')
        try:
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b'\0')
                stream.flush()
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.seek(1)
            try:
                owner = json.loads(stream.read().decode('utf-8')).get('pid', '?')
            except (ValueError, OSError):
                owner = '?'
            stream.close()
            raise ProfileError(f'资料库正在由其他实例使用（PID {owner}），请关闭该实例后重试：' + str(self.root)) from None
        stream.seek(1)
        stream.truncate()
        stream.write(json.dumps(dict(pid=os.getpid()), ensure_ascii=True).encode('utf-8'))
        stream.flush()
        self._stream = stream
        return self

    def release(self):
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.release()
