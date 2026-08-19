
#########################################################
# md5crypt.py (Python 3 verze)
#
# Převod původní implementace (2000, Michal Wallace)
# na čistý Python 3
#
#########################################################

import hashlib

MAGIC = '$1$'  # Magic string
ITOA64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def to64(v, n):
    """Převede číslo v na base64 variantu (64 znaků z ITOA64)"""
    ret = ''
    while n > 0:
        ret += ITOA64[v & 0x3f]
        v >>= 6
        n -= 1
    return ret


def apache_md5_crypt(pw: str, salt: str):
    """Apache varianta md5crypt (používá $apr1$ místo $1$)"""
    return unix_md5_crypt(pw, salt, magic='$apr1$')


def unix_md5_crypt(pw: str, salt: str, magic: str = None) -> str:
    """Unix MD5 crypt – kompatibilní s crypt(3)"""
    if magic is None:
        magic = MAGIC

    if salt.startswith(magic):
        salt = salt[len(magic):]

    salt = salt.split('$', 1)[0]
    salt = salt[:8]  # maximálně 8 znaků

    pw_bytes = pw.encode("utf-8")
    salt_bytes = salt.encode("utf-8")
    magic_bytes = magic.encode("utf-8")

    ctx = pw_bytes + magic_bytes + salt_bytes
    final = hashlib.md5(pw_bytes + salt_bytes + pw_bytes).digest()

    # přidej MD5 bloky podle délky hesla
    i = len(pw_bytes)
    while i > 0:
        if i > 16:
            ctx += final[:16]
        else:
            ctx += final[:i]
        i -= 16

    # divné XORování znaků
    i = len(pw_bytes)
    while i > 0:
        if i & 1:
            ctx += b"\x00"
        else:
            ctx += pw_bytes[:1]
        i >>= 1

    final = hashlib.md5(ctx).digest()

    # zpomalení – 1000x míchat MD5
    for i in range(1000):
        ctx1 = b""
        if i & 1:
            ctx1 += pw_bytes
        else:
            ctx1 += final
        if i % 3:
            ctx1 += salt_bytes
        if i % 7:
            ctx1 += pw_bytes
        if i & 1:
            ctx1 += final
        else:
            ctx1 += pw_bytes
        final = hashlib.md5(ctx1).digest()

    # finální transformace na 22 znaků
    passwd = ""
    passwd += to64(((final[0] << 16) | (final[6] << 8) | final[12]), 4)
    passwd += to64(((final[1] << 16) | (final[7] << 8) | final[13]), 4)
    passwd += to64(((final[2] << 16) | (final[8] << 8) | final[14]), 4)
    passwd += to64(((final[3] << 16) | (final[9] << 8) | final[15]), 4)
    passwd += to64(((final[4] << 16) | (final[10] << 8) | final[5]), 4)
    passwd += to64(final[11], 2)

    return magic + salt + '$' + passwd


# wrapper funkce
md5crypt = unix_md5_crypt


if __name__ == "__main__":
    # test
    print(unix_md5_crypt("cat", "hat"))
