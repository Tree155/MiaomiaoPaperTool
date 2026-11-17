源码说明（Source Notes）
============================

中文： ..\docs\源码说明.pdf
English: ..\docs\Source-NOTES-EN.pdf

运行：
    pip install -r requirements.txt
    python desktop.py

打包 exe：
    双击 重新打包.bat

打发布包（GitHub Release 用，中英各一个）：
    python pack_release.py zh v1.4.2        -> MiaomiaoTool-v1.4.2-zh.zip
    python pack_release.py en v1.4.2-en     -> MiaomiaoTool-v1.4.2-en.zip

英文版（English build）
-----------------------
en\ 目录是英文版源码（界面 / 日志 / 报错 / 导出表头全英文，默认目录名 library\ 与 export\）。
它不是手写的，由上级目录的中文源码生成：

    python tools/i18n/make_en.py          # 在仓库根目录跑

改代码请改本目录（中文版），然后重跑上面这条命令重新生成 en\。
详见 tools/i18n/README.md。打包英文版：双击 en\build.bat。
