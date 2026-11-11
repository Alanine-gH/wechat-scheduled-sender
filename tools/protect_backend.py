# -*- coding: utf-8 -*-
"""构建期：把 backend/ 自有模块编译加密成 core.bin，并生成 sourceless _boot.pyc。

设计前提（重要，别误读成「加密保护」）：
  Python 是解释型语言，运行时必须有字节码。密钥必然随包分发，
  有经验的逆向者可以解密或反编译 _boot.pyc。这里做的是：
    * 安装目录里不再有任何自有 .py 明文（23 个文件消失）
    * 字节码用随机 keystream 混淆，不是可直接反编译的标准 .pyc
    * 提高随手翻看/改代码的门槛
  它**不是**防逆向的加密保护，别对客户这样宣传。

运行期链路：
  python.exe <resources>/backend/_boot.pyc 0 --token ... --quiet --version ...
    -> _boot.pyc 解密 core.bin
    -> 注册 MetaPathFinder，从内存 code object 提供 23 个模块
    -> 以 __main__ 执行 server 的 code object

为什么用标准库：core.bin 解密发生在任何第三方库 import 之前，
不能依赖 cryptography / Crypto（它们也可能被清理）。
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import marshal
import os
import py_compile
import shutil
import sys
import tempfile
import zlib

# 启动引导脚本。它会和 core.bin 一起被打进 resources/backend/。
# 注意：这里是**明文源码**，构建后只保留编译出的 .pyc，源码不进包。
BOOT_SOURCE = r'''# -*- coding: utf-8 -*-
# sourceless bootloader. No .py ships with the app.
import sys, os, marshal, zlib, hmac, hashlib, types

_HERE = os.path.dirname(os.path.abspath(__file__))
_KEY = __KEY__

def _decrypt(blob):
    if len(blob) < 48:
        raise RuntimeError('core.bin truncated')
    nonce, tag, ct = blob[:16], blob[16:48], blob[48:]
    if not hmac.compare_digest(
            hmac.new(_KEY, nonce + ct, hashlib.sha256).digest(), tag):
        raise RuntimeError('core.bin integrity check failed')
    n = (len(ct) + 31) // 32
    ks = b''.join(hmac.new(_KEY, nonce + i.to_bytes(4, 'big'),
                           hashlib.sha256).digest() for i in range(n))
    return zlib.decompress(bytes(a ^ b for a, b in zip(ct, ks[:len(ct)])))

class _Loader:
    def __init__(self, codes, here):
        self.codes = codes
        self.here = here
    def find_spec(self, fullname, path=None, target=None):
        if fullname not in self.codes:
            return None
        # 用 spec_from_loader 造一个普通模块 spec；is_package=False
        from importlib.util import spec_from_loader
        return spec_from_loader(fullname, self, is_package=False)
    def create_module(self, spec):
        return None
    def exec_module(self, module):
        name = module.__name__
        code = marshal.loads(self.codes[name])
        # 保留原 __file__ 语义：server.py 用它算 sys.path 与 frontend 目录，
        # userdata.py 用它算 project_root()。伪装成同目录下的 .py。
        module.__file__ = os.path.join(self.here, name.replace('.', '/') + '.py')
        module.__package__ = ''
        exec(code, module.__dict__)

def _boot():
    blob = open(os.path.join(_HERE, 'core.bin'), 'rb').read()
    payload = marshal.loads(_decrypt(blob))
    codes = payload['codes']
    main_code = payload['__main__']

    # backend 目录里现在没有 .py 了，确保它仍在 sys.path（server.py 会再插一次）
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    sys.meta_path.insert(0, _Loader(codes, _HERE))

    g = {
        '__name__': '__main__',
        '__file__': os.path.join(_HERE, 'server.py'),
        '__package__': None,
        '__spec__': None,
        '__builtins__': __builtins__,
        '__loader__': None,
    }
    # sys.argv 由 python.exe 传入（脚本名是 _boot.pyc），改成 server.py 更贴近原语义
    if sys.argv:
        sys.argv[0] = g['__file__']
    exec(marshal.loads(main_code), g)

_boot()
'''

# 允许进包的文件白名单之外的自有模块（server 作为 __main__ 单独放）
SKIP = set()


def modules(src_dir):
    out = []
    for name in sorted(os.listdir(src_dir)):
        if not name.endswith('.py') or name.startswith('_'):
            continue
        if name in SKIP:
            continue
        out.append(name)
    return out


def compile_code(path, fake_name):
    """编译成 code object，并把 co_filename 伪装成包内路径（不泄露开发机路径）。"""
    src = open(path, 'r', encoding='utf-8').read()
    code = compile(src, fake_name, 'exec')
    return marshal.dumps(code)


def build(src_dir, out_dir, key=None):
    os.makedirs(out_dir, exist_ok=True)
    for f in os.listdir(out_dir):
        p = os.path.join(out_dir, f)
        if os.path.isfile(p):
            os.remove(p)

    key = key or os.urandom(32)
    if len(key) != 32:
        raise ValueError('key must be 32 bytes')

    names = modules(src_dir)
    if 'server.py' not in names:
        raise RuntimeError('server.py 缺失')
    codes = {}
    for name in names:
        if name == 'server.py':
            continue
        mod = name[:-3]
        codes[mod] = compile_code(os.path.join(src_dir, name),
                                  'backend/' + name)
    main_code = compile_code(os.path.join(src_dir, 'server.py'),
                             'backend/server.py')

    payload = marshal.dumps({'codes': codes, '__main__': main_code})
    raw = zlib.compress(payload, 9)
    nonce = os.urandom(16)
    n = (len(raw) + 31) // 32
    ks = b''.join(hmac.new(key, nonce + i.to_bytes(4, 'big'),
                           hashlib.sha256).digest() for i in range(n))
    ct = bytes(a ^ b for a, b in zip(raw, ks[:len(raw)]))
    tag = hmac.new(key, nonce + ct, hashlib.sha256).digest()
    with open(os.path.join(out_dir, 'core.bin'), 'wb') as f:
        f.write(nonce + tag + ct)

    # sourceless _boot.pyc：先写临时 .py，编译成 legacy 布局的 .pyc，再删 .py
    boot_src = BOOT_SOURCE.replace('__KEY__', repr(key))
    tmp = tempfile.mkdtemp(prefix='wcs-boot-')
    try:
        boot_py = os.path.join(tmp, '_boot.py')
        open(boot_py, 'w', encoding='utf-8').write(boot_src)
        py_compile.compile(boot_py, cfile=os.path.join(out_dir, '_boot.pyc'),
                           doraise=True, optimize=2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return {
        'modules': len(names),
        'core_bytes': os.path.getsize(os.path.join(out_dir, 'core.bin')),
        'boot_bytes': os.path.getsize(os.path.join(out_dir, '_boot.pyc')),
        'key_hex': key.hex(),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--python', default=sys.executable)
    a = ap.parse_args()
    info = build(a.src, a.out)
    print('[protect] modules=%d core=%d bytes boot=%d bytes'
          % (info['modules'], info['core_bytes'], info['boot_bytes']))
    print('[protect] 注意：密钥随包分发，这不是防逆向的加密保护')


if __name__ == '__main__':
    main()
