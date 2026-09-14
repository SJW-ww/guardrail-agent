"""口令散列。

用标准库的 `scrypt` 而不是 bcrypt/argon2 —— 目的是零新增依赖、且不把
「治理内核」的信任边界推到第三方 C 扩展上。scrypt 是内存硬的:
它把暴力破解的成本从「算得快」变成「记得多」,这正是我们要的。

存储格式自带参数:`scrypt$n$r$p$salt$hash`。
参数升级后老口令仍能校验(照它自己的参数算),不需要强制全员改密。
"""

import base64
import hashlib
import hmac
import secrets

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_DKLEN = 32
_MAXMEM = 64 * 1024 * 1024


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_DKLEN,
        maxmem=_MAXMEM,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(derived)}"


def verify_password(password: str, stored: str) -> bool:
    """校验口令。格式不对一律当失败,绝不抛异常 —— 认证入口不能因为脏数据 500。"""
    try:
        scheme, n_raw, r_raw, p_raw, salt_raw, expected_raw = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_raw), int(r_raw), int(p_raw)
        salt = base64.b64decode(salt_raw)
        expected = base64.b64decode(expected_raw)
    except (ValueError, TypeError):
        return False

    try:
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=_MAXMEM,
        )
    except (ValueError, OverflowError):
        # 参数被写坏(比如 n 不是 2 的幂)时不能把服务打挂
        return False

    # 定长比较:别让「第几位开始不一样」泄露出去
    return hmac.compare_digest(derived, expected)
