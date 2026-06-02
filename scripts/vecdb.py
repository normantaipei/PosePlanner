#!/usr/bin/env python3
"""sqlite-vec 載入層 —— 讓 library.db 具備向量搜尋能力。

設計重點
────────
1) **二進位 vendored 在專案裡**（`vendor/sqlite-vec/`），依執行平台挑檔，
   直接 `conn.load_extension(...)` 載入。**不需要 pip 裝 sqlite_vec 套件**，
   也不需要網路，整個庫自我包含。
2) **Python 必須支援 loadable extension**。macOS 內建的 /usr/bin/python3
   被 Apple 編成「沒有 enable_load_extension」，無法載入 sqlite-vec。
   若當前直譯器不支援，`ensure_capable_interpreter()` 會自動找一個支援的
   python3（如 Homebrew 的）並 re-exec 過去，使用者照樣 `python3 scripts/...` 即可。
"""
from __future__ import annotations

import os
import platform
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = ROOT / "vendor" / "sqlite-vec"

# 防止 re-exec 無限迴圈的旗標
_REEXEC_FLAG = "POSEPLANNER_VEC_REEXECED"


def vec_binary_path() -> Path:
    """依當前平台回傳 vendored 的 vec0 二進位路徑。可用 POSEPLANNER_VEC_BIN 覆寫。"""
    override = os.environ.get("POSEPLANNER_VEC_BIN")
    if override:
        return Path(override).expanduser()

    system = platform.system()           # 'Darwin' / 'Linux'
    machine = platform.machine().lower()  # 'arm64'/'aarch64' / 'x86_64'/'amd64'
    arm = machine in ("arm64", "aarch64")
    x86 = machine in ("x86_64", "amd64")

    if system == "Darwin":
        name = "vec0.macos-aarch64.dylib" if arm else "vec0.macos-x86_64.dylib"
    elif system == "Linux":
        name = "vec0.linux-aarch64.so" if arm else "vec0.linux-x86_64.so"
    else:
        raise RuntimeError(
            f"沒有為此平台 vendor sqlite-vec 二進位：{system}/{machine}。"
            f"請到 https://github.com/asg017/sqlite-vec/releases 下載對應檔，"
            f"放進 {VENDOR_DIR}/，或用 POSEPLANNER_VEC_BIN 指定路徑。"
        )

    path = VENDOR_DIR / name
    if not path.exists():
        # 退而求其次：x86/arm 任一存在就用（涵蓋命名差異）
        for alt in VENDOR_DIR.glob("vec0.*"):
            if (system.lower() in alt.name) and (
                ("aarch64" in alt.name) == arm or ("x86_64" in alt.name) == x86
            ):
                return alt
        raise FileNotFoundError(
            f"找不到 vendored 二進位：{path}。"
            f"現有：{[p.name for p in VENDOR_DIR.glob('vec0.*')] or '（空）'}"
        )
    return path


def load_vec(conn: sqlite3.Connection) -> None:
    """把 sqlite-vec 載入到一條連線上。呼叫前直譯器須支援 enable_load_extension。"""
    if not hasattr(conn, "enable_load_extension"):
        raise RuntimeError(
            "目前的 Python sqlite3 不支援 loadable extension。"
            "請改用支援的 Python（見 ensure_capable_interpreter）。"
        )
    bin_path = vec_binary_path()
    conn.enable_load_extension(True)
    # load_extension 會自動補副檔名，但我們給完整檔名最保險；去掉副檔名讓 SQLite 自己補
    stem = str(bin_path.with_suffix(""))
    try:
        conn.load_extension(stem)
    except sqlite3.OperationalError:
        conn.load_extension(str(bin_path))  # 退回用完整檔名
    finally:
        conn.enable_load_extension(False)


def _interpreter_supports_extensions(py: str) -> bool:
    """檢查某個 python 可執行檔的 sqlite3 是否支援 enable_load_extension。"""
    import subprocess

    code = (
        "import sqlite3,sys;"
        "c=sqlite3.connect(':memory:');"
        "sys.exit(0 if hasattr(c,'enable_load_extension') else 1)"
    )
    try:
        return subprocess.run([py, "-c", code], capture_output=True).returncode == 0
    except Exception:
        return False


def _find_capable_python() -> str | None:
    """找一個 sqlite3 支援 extension loading 的 python3。"""
    candidates: list[str] = []
    env = os.environ.get("POSEPLANNER_PYTHON")
    if env:
        candidates.append(env)
    # Homebrew（Apple Silicon / Intel）與常見版本
    candidates += [
        "/opt/homebrew/bin/python3.13",
        "/opt/homebrew/bin/python3.12",
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3.13",
        "/usr/local/bin/python3.12",
        "/usr/local/bin/python3",
    ]
    for name in ("python3.13", "python3.12", "python3.11", "python3"):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    seen: set[str] = set()
    for c in candidates:
        rc = os.path.realpath(c) if os.path.exists(c) else c
        if rc in seen:
            continue
        seen.add(rc)
        if os.path.exists(c) and _interpreter_supports_extensions(c):
            return c
    return None


def ensure_capable_interpreter() -> None:
    """若當前直譯器無法載入 extension，re-exec 到一個可以的 python3。

    在每個會用到向量功能的腳本 main() 最前面呼叫即可。找不到可用直譯器就明確報錯。
    """
    probe = sqlite3.connect(":memory:")
    if hasattr(probe, "enable_load_extension"):
        probe.close()
        return
    probe.close()

    if os.environ.get(_REEXEC_FLAG):
        # 已經 re-exec 過還是不行 → 別再跳，直接報錯
        raise SystemExit(
            "目前的 Python（"
            + sys.executable
            + "）sqlite3 不支援 loadable extension，且找不到替代直譯器。\n"
            "請安裝一個支援的 Python（例如 `brew install python`），"
            "或用環境變數 POSEPLANNER_PYTHON 指定其路徑。"
        )

    capable = _find_capable_python()
    if not capable:
        raise SystemExit(
            "需要 sqlite-vec，但此環境的 Python sqlite3 不支援 loadable extension，"
            "且找不到支援的替代 python3。\n"
            "macOS 內建 /usr/bin/python3 不支援，請 `brew install python` 後重試，"
            "或設定 POSEPLANNER_PYTHON 指向可用直譯器。"
        )

    env = dict(os.environ, **{_REEXEC_FLAG: "1"})
    os.execve(capable, [capable, *sys.argv], env)


if __name__ == "__main__":
    # 自我測試：python3 scripts/vecdb.py
    ensure_capable_interpreter()
    conn = sqlite3.connect(":memory:")
    load_vec(conn)
    (ver,) = conn.execute("select vec_version()").fetchone()
    print(f"OK  直譯器={sys.executable}")
    print(f"    binary={vec_binary_path()}")
    print(f"    vec_version={ver}")
