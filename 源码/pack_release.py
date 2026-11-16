"""
组装发布包（中文版 / 英文版各一）。

用法（在仓库根目录跑）：

    python 源码/pack_release.py zh v1.4.2
    python 源码/pack_release.py en v1.4.2-en

产物：仓库根目录下
    文献妙妙小工具-v1.4.2-zh.zip   中文版（含中文说明）
    MiaomiaoTool-v1.4.2-en.zip        English version (English docs only)

两版**功能完全相同**，差别只有附带的文档语言；exe 在英文包里改名为 MiaomiaoTool.exe
（程序靠 sys.executable 定位目录，改名字不影响运行）。

为什么用这个脚本而不是 PowerShell 的 Compress-Archive：
Windows 的 Compress-Archive 对非 ASCII 文件名会用系统代码页写 zip，
在 GitHub Actions 上容易把中文文件名写成乱码。Python 的 zipfile 固定 UTF-8，最稳。
"""

from __future__ import annotations

import pathlib
import shutil
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent


def find_exe(lang: str = "zh") -> pathlib.Path:
    """CI 里在 源码/[en/]dist/ 下；本地在仓库根目录。"""
    order = ([ROOT / "源码" / "en" / "dist" / "MiaomiaoTool.exe"] if lang == "en" else []) + [
        ROOT / "源码" / "dist" / "MiaomiaoTool.exe",
        ROOT / "文献妙妙小工具.exe",
        ROOT / "源码" / "dist" / "文献妙妙小工具.exe",
        ROOT / "MiaomiaoTool.exe",
    ]
    for c in order:
        if c.is_file():
            return c
    raise SystemExit("找不到 exe：先打包（源码/重新打包.bat、源码/en/build.bat 或 PyInstaller）")


def build(lang: str, tag: str) -> pathlib.Path:
    lang = lang.lower()
    if lang not in ("zh", "en"):
        raise SystemExit(f"语言只能是 zh 或 en，收到 {lang!r}")

    stage = ROOT / "_release_stage"
    shutil.rmtree(stage, ignore_errors=True)
    (stage / "docs").mkdir(parents=True)

    exe = find_exe(lang)
    if lang == "zh":
        shutil.copy2(exe, stage / "文献妙妙小工具.exe")
        docs = [("使用说明.pdf", "使用说明.pdf"),
                ("docs/源码说明.pdf", "docs/源码说明.pdf")]
        cfg = ROOT / "config.example.json"
        zip_name = f"文献妙妙小工具-{tag}.zip"
    else:
        shutil.copy2(exe, stage / "MiaomiaoTool.exe")
        docs = [("User-Guide-EN.pdf", "User-Guide-EN.pdf"),
                ("docs/Source-NOTES-EN.pdf", "docs/Source-NOTES-EN.pdf")]
        cfg = ROOT / "config.example-en.json"
        zip_name = f"MiaomiaoTool-{tag}.zip"

    # 空配置：用户解压后只要填 email
    if not cfg.is_file():
        raise SystemExit(f"缺配置样例：{cfg}")
    shutil.copy2(cfg, stage / "config.json")

    for src, dst in docs:
        s = ROOT / src
        if not s.is_file():
            raise SystemExit(f"缺文档：{s}")
        shutil.copy2(s, stage / dst)

    out = ROOT / zip_name
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(stage.rglob("*")):
            if f.is_file():
                z.write(f, arcname=str(f.relative_to(stage)))

    shutil.rmtree(stage, ignore_errors=True)
    return out


def main() -> None:
    lang = sys.argv[1] if len(sys.argv) > 1 else "zh"
    tag = sys.argv[2] if len(sys.argv) > 2 else "dev"
    out = build(lang, tag)
    with zipfile.ZipFile(out) as z:
        print(f"{out.name}  {out.stat().st_size / 1024 / 1024:.2f} MB")
        for i in z.infolist():
            print(f"  {i.file_size / 1024:9.1f} KB  {i.filename}")


if __name__ == "__main__":
    main()
