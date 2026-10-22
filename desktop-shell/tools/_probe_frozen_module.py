"""从 PyInstaller 冻结 exe 中提取指定模块的字节码，验证新代码是否入包。

用法: python _probe_pyz_module.py <exe路径> <模块虚路径，如 desktop.desktop.native_menu_backend>
原理: 解析 CArchive 尾部 cookie → 定位 PYZ-00.pyz → 解析 PYZ TOC →
      zlib 解压目标模块的 marshalled code → 按字节搜索特征字符串。
"""
import struct
import sys
import zlib

MAGIC = b"MEI\014\013\012\013\016"


def find_cookie(data: bytes) -> dict:
    idx = data.rfind(MAGIC)
    if idx < 0:
        raise SystemExit("no CArchive cookie found")
    # magic(8) + lengthofPackage(4) + toc(4) + tocLen(4) + pyvers(4) + pylibname(64)
    _, pkg_len, toc_off, toc_len, pyvers = struct.unpack(
        "!8sIIII", data[idx : idx + 24]
    )
    pylib = data[idx + 24 : idx + 88].rstrip(b"\x00").decode("ascii", "replace")
    pkg_start = len(data) - pkg_len
    return {
        "pkg_start": pkg_start,
        "toc_off": toc_off,
        "toc_len": toc_len,
        "pyvers": pyvers,
        "pylib": pylib,
    }


def read_toc(data: bytes, c: dict):
    toc = []
    base = c["pkg_start"] + c["toc_off"]
    end = base + c["toc_len"]
    pos = base
    while pos < end:
        (entry_size,) = struct.unpack("!I", data[pos : pos + 4])
        entry_pos, cmpr_size, uncmpr_size, flag = struct.unpack(
            "!IIIB", data[pos + 4 : pos + 17]
        )
        typ = data[pos + 17 : pos + 18].decode("ascii", "replace")
        name = data[pos + 18 : pos + entry_size].rstrip(b"\x00").decode("utf-8", "replace")
        toc.append((name, typ, entry_pos, cmpr_size, uncmpr_size, flag))
        pos += entry_size
    return toc


def extract_pyz(data: bytes, c: dict, toc):
    for name, typ, entry_pos, cmpr_size, uncmpr_size, flag in toc:
        if typ == "z" or name.endswith("PYZ.pyz") or name.endswith("PYZ-00.pyz"):
            raw = data[c["pkg_start"] + entry_pos : c["pkg_start"] + entry_pos + cmpr_size]
            return zlib.decompress(raw) if flag else raw
    raise SystemExit("PYZ archive not found in CArchive")


def read_pyz_toc(pyz: bytes):
    # PyInstaller >= 6.2 头布局: b'PYZ\0' + BYTECODE_MAGIC(4) + toc_offset(!i, 4) + 4 unused
    toc_off = struct.unpack("!i", pyz[8:12])[0]
    import marshal

    return marshal.loads(pyz[toc_off:])


def main() -> int:
    exe, module = sys.argv[1], sys.argv[2]
    with open(exe, "rb") as fh:
        data = fh.read()
    c = find_cookie(data)
    toc = read_toc(data, c)
    pyz = extract_pyz(data, c, toc)
    names = read_pyz_toc(pyz)
    target = None
    for entry in names:  # TOC 是 (name, (typ, pos, length)) 元组的列表
        name, (typ, pos, length) = entry
        if name == module or name.replace("/", ".").endswith(module):
            target = (name, pos, length)
            break
    if target is None:
        print(f"MODULE NOT FOUND: {module}")
        print("candidates:", [n for n in names if "native_menu" in n or "menubar" in n])
        return 1
    name, pos, length = target
    blob = zlib.decompress(pyz[pos : pos + length])
    markers = {
        "NEW _apply_dropdown_font": b"_apply_dropdown_font",
        "NEW log msg": b"dropdown font apply failed",
        "shared old log msg": b"menu strip ready",
        "shared dropdown comment-adjacent": b"ShowImageMargin",
    }
    print(f"module: {name}")
    print(f"python: {c['pylib']} (pyvers {c['pyvers']})")
    print(f"decompressed size: {len(blob)} bytes")
    verdict_new = True
    for label, needle in markers.items():
        found = needle in blob
        print(f"  [{'FOUND' if found else '  -  '}] {label}: {needle.decode('ascii', 'replace')}")
        if label.startswith("NEW") and not found:
            verdict_new = False
    print("VERDICT:", "NEW CODE INCLUDED" if verdict_new else "OLD CODE (fix missing)")
    return 0 if verdict_new else 2


if __name__ == "__main__":
    raise SystemExit(main())
