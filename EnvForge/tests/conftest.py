import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.dont_write_bytecode = True

from envforge.db import Database


class MemoryProvider:
    """测试用环境变量提供者，完全不触碰注册表。"""

    def __init__(self, scope="user", values=None, writable=True):
        self.scope = scope
        self.values = dict(values or {})
        self.writable = writable

    def list_vars(self):
        return dict(self.values)

    def get(self, name):
        return self.values.get(name)

    def write(self, updates):
        from envforge.errors import EnvAccessError

        if not self.writable:
            raise EnvAccessError(f"{self.scope} 作用域只读")
        for key, value in updates.items():
            if value is None:
                self.values.pop(key, None)
            else:
                self.values[key] = value


class FakeCipher:
    """可用的假加密器：前缀 enc: + base64，便于断言“密文不含明文”。"""

    def __init__(self, available=True):
        self.available = available
        self.unavailable_reason = "" if available else "测试环境不可用"

    def status(self):
        return "FakeCipher"

    def protect(self, value):
        import base64

        return b"enc:" + base64.b64encode(value.encode("utf-8"))

    def unprotect(self, blob):
        import base64

        from envforge.secrets import CryptoError

        text = blob.decode("utf-8")
        if not text.startswith("enc:"):
            raise CryptoError("密文格式错误")
        return base64.b64decode(text[4:]).decode("utf-8")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "envforge-test.db")
    yield database
    database.close()


@pytest.fixture
def cipher():
    return FakeCipher()


@pytest.fixture
def provider():
    return MemoryProvider("user", {"PATH": r"C:\Windows;C:\Windows\System32", "USERNAME": "tester"})


@pytest.fixture
def env_service(db, provider):
    from envforge.envstore import EnvService

    system = MemoryProvider("system", {"PATH": r"C:\Windows;C:\Windows\System32"}, writable=False)
    session = MemoryProvider("session", {"PATH": r"C:\Windows"}, writable=True)
    return EnvService(db, {"user": provider, "system": system, "session": session})
