English build of Miaomiao Paper Tool
=====================================

This folder holds the **fully English build**: every UI string, log line,
error message, Excel column header, dialog and default folder name has been
translated. Nothing in the running program is Chinese except the parts that
come from external services (easyScholar returns CAS zones in Chinese).

The Python sources here are *generated* from the Chinese sources one level up
by `tools/i18n/make_en.py` (see the repository root). Each file has an
identical abstract syntax tree to its Chinese counterpart -- only string
literals and comments differ. **Do not edit these files by hand**: edit the
Chinese sources and re-run the generator.

Run it
------
    pip install -r ..\requirements.txt
    python desktop.py

Build the exe
-------------
    double-click build.bat          (needs pyinstaller: pip install pyinstaller)

The build script writes MiaomiaoTool.exe two levels up (repository root).

Differences from the Chinese build
----------------------------------
| | Chinese build | English build |
|---|---|---|
| UI / logs / errors | Chinese | English |
| Library folder | `文献库\` | `library\` |
| Export folder | `导出\` | `export\` |
| To-download list | `待下载清单.html` | `to-download.html` |
| Zotero collection prefix | `小工具导入` | `Miaomiao Import` |
| Config key for the note | `_说明` | `_notes` |

Data folders are **not** interchangeable between the two builds; point
`library_dir` in `config.json` at the other folder if you want to share a
library.
