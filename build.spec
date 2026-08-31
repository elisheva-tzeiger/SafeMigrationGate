# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build for SafeMigrationGate.

Build with:  pyinstaller build.spec --noconfirm

Three traps handled here:
  1. Bundled resources are resolved through agent.resources.resource_path, which
     reads sys._MEIPASS. The `datas` paths below must match the strings passed to
     resource_path exactly ("agent/knowledge", "data/demo.db", "evals/rubric.md").
  2. SQLite opens a journal even for a transaction that only rolls back, and the
     bundle directory is read-only. ensure_demo_db() copies the database to
     %LOCALAPPDATA%\\SafeMigrationGate on first run; nothing to do here.
  3. sqlglot's dialects and several langgraph components are imported by name at
     run time, so the static scan misses them. Hence hiddenimports.
"""

block_cipher = None

datas = [
    ("agent/knowledge", "agent/knowledge"),
    ("data/demo.db", "data"),
    ("evals/rubric.md", "evals"),
]

hiddenimports = [
    "sqlglot.dialects",
    "sqlglot.dialects.postgres",
    "sqlglot.dialects.mysql",
    "sqlglot.dialects.tsql",
    "sqlglot.dialects.sqlite",
    "sqlglot.dialects.oracle",
    "langgraph",
    "langgraph.graph",
    "langgraph.checkpoint.memory",
    "langgraph.types",
    "langchain_openai",
    "truststore",
    "rank_bm25",
    "sqlalchemy.dialects.sqlite",
]

a = Analysis(
    ["agent/cli.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "IPython", "pytest"],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    onefile=True,
)
