"""fa_aligner.build_env 单测：ffmpeg 兜底 PATH 注入。

核心场景（这就是线上会踩的那个坑）：
  系统 PATH 里没有 ffmpeg，但 <app>/ffmpeg/ffmpeg.exe 存在。
  旧代码不传 env → main.exe 解码失败 → 静默返回 0~2s 的兜底对齐。
  新代码应把 <app>/ffmpeg 前置到子进程 PATH。
"""
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(r"C:\Green\QwenASR-WebView.dev\01_repo")
sys.path.insert(0, str(REPO))

import app_paths                      # noqa: E402
import fa_aligner                     # noqa: E402

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond), extra))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (" - " + str(extra) if extra else ""))


def with_path(path_value, fn):
    """临时改写 PATH，跑完还原。"""
    saved = os.environ.get("PATH")
    os.environ["PATH"] = path_value
    try:
        return fn()
    finally:
        if saved is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = saved


def first_entry(env):
    v = env.get("PATH") or env.get("Path") or ""
    return v.split(os.pathsep)[0] if v else ""


def main():
    print("[1] 系统 PATH 有 ffmpeg 时，不改动原有优先顺序")
    real = shutil_which = None
    import shutil
    real = shutil.which("ffmpeg")
    if real:
        env = fa_aligner.build_env()
        check("保留系统 ffmpeg 目录在首位",
              first_entry(env).lower() == str(Path(real).parent).lower(),
              f"{first_entry(env)} vs {Path(real).parent}")
    else:
        print("  SKIP  本机 PATH 无 ffmpeg")

    print("\n[2] 系统 PATH 无 ffmpeg、app 目录有 ffmpeg 时应注入")
    fake_app = Path(tempfile.mkdtemp(prefix="faenv_"))
    (fake_app / "ffmpeg").mkdir(parents=True, exist_ok=True)
    (fake_app / "ffmpeg" / "ffmpeg.exe").write_bytes(b"stub")

    orig_app_dir = app_paths.app_dir
    app_paths.app_dir = lambda: fake_app
    try:
        # ffmpeg_utils 在 import 时已绑定 app_paths，这里直接改模块属性
        import ffmpeg_utils
        orig_find = ffmpeg_utils.find_ffmpeg
        ffmpeg_utils.find_ffmpeg = lambda: fake_app / "ffmpeg" / "ffmpeg.exe"
        try:
            env = with_path(r"C:\Windows\System32", fa_aligner.build_env)
            check("PATH 首项被替换为 app 的 ffmpeg 目录",
                  first_entry(env).lower() == str(fake_app / "ffmpeg").lower(),
                  first_entry(env))
            check("windows\\system32 仍在 PATH 中",
                  "system32" in (env.get("PATH") or env.get("Path")).lower())
            check("FFMPEG_PATH 已设置",
                  (env.get("FFMPEG_PATH") or "").endswith("ffmpeg.exe"),
                  env.get("FFMPEG_PATH"))
        finally:
            ffmpeg_utils.find_ffmpeg = orig_find
    finally:
        app_paths.app_dir = orig_app_dir

    print("\n[3] 都找不到时不应破坏原环境")
    import ffmpeg_utils
    orig_find = ffmpeg_utils.find_ffmpeg
    ffmpeg_utils.find_ffmpeg = lambda: None
    try:
        before = os.environ.get("PATH")
        env = fa_aligner.build_env()
        after = env.get("PATH") or env.get("Path")
        check("PATH 保持原样", before == after, f"{len(before or '')}B")
        check("不设置 FFMPEG_PATH", "FFMPEG_PATH" not in env)
    finally:
        ffmpeg_utils.find_ffmpeg = orig_find

    ok = sum(1 for _, c, _ in RESULTS if c)
    print(f"\nResult: {ok}/{len(RESULTS)} passed")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())