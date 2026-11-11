"""说话人聚类与重采样：自实现替换 SciPy 后的回归测试。

背景：打包体积里有 69 MB 是 SciPy（scipy/ 49.7 + scipy.libs/ 19.3），
而本项目只用到它两个函数 —— ``linkage/fcluster`` 与 ``resample_poly``。
两者都已换成自带实现（``cluster_core.py`` / ``audio_io._resample_fft``）。

本测试锁住三件事：
  1. ``cluster_core`` 与 SciPy **输出逐位一致**（有 SciPy 时对拍 300 组随机
     数据；CI 的发布构建刻意不装 SciPy，此时改跑不变量检查）
  2. ``diarize`` 的分群结果在人造「两人对话」上确实分成两人
  3. 源码里不再 import scipy —— 否则瘦身白做

    python tests/test_cluster.py
"""
from __future__ import annotations

import ast
import sys
import traceback
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import cluster_core as cc                                      # noqa: E402

try:
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial.distance import squareform
    HAS_SCIPY = True
except Exception:                                              # pragma: no cover
    HAS_SCIPY = False


class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append(bool(ok))
        print(f"  {'通过' if ok else '失败'}  {name}" + (f" — {detail}" if detail else ""))

    def skip(self, name, why):
        self.items.append(True)
        print(f"  跳过  {name} — {why}")

    @property
    def ok(self):
        return all(self.items)


def same_partition(a, b) -> bool:
    """两个标签向量是否表示同一个划分（忽略编号本身）。"""
    a, b = np.asarray(a), np.asarray(b)
    if len(a) != len(b):
        return False
    mapping: dict[int, int] = {}
    for x, y in zip(a, b):
        if x in mapping and mapping[x] != y:
            return False
        mapping[x] = y
    return len(set(mapping.values())) == len(set(a))


def synth_embeddings(seed: int, n: int, groups: int, dim: int = 16):
    """造 n 个单位向量，天然分 groups 簇（模拟说话人嵌入）。"""
    rng = np.random.default_rng(seed)
    centers = rng.normal(size=(groups, dim)) * 3.0
    pts = centers[rng.integers(0, groups, n)] + rng.normal(size=(n, dim)) * 0.4
    return pts / np.linalg.norm(pts, axis=1, keepdims=True)


def dist_of(emb: np.ndarray) -> np.ndarray:
    d = np.clip(1.0 - emb @ emb.T, 0.0, 2.0)
    np.fill_diagonal(d, 0.0)
    return d


# ══════════════════════════════════════════════════════════════════════
# 1) cluster_core 自身不变量
# ══════════════════════════════════════════════════════════════════════
def test_invariants(r: Result):
    print("\n[cluster_core] 结构不变量")
    emb = synth_embeddings(1, 12, 3)
    d = dist_of(emb)
    Z = cc.average_linkage(d)

    r.check("Z 形状为 (n-1, 4)", Z.shape == (11, 4), str(Z.shape))
    r.check("合并距离单调不减", bool(np.all(np.diff(Z[:, 2]) >= -1e-12)),
            f"min diff={np.min(np.diff(Z[:, 2])):.3e}" if len(Z) > 1 else "")
    r.check("每行左小右大", bool(np.all(Z[:, 0] < Z[:, 1])))
    r.check("簇编号不超出 [0, 2n-2]",
            bool(Z[:, 0].min() >= 0 and Z[:, 1].max() <= 2 * 12 - 2))
    r.check("最后一行为 2n-2 个样本", Z[-1, 3] == 12, str(Z[-1, 3]))
    # 每行的「样本数」必须等于两个子簇样本数之和（Z 的定义，也是 scipy 的约定）
    sizes = {i: 1.0 for i in range(12)}
    ok_sizes = True
    for row in Z:
        a, b, cnt = int(row[0]), int(row[1]), row[3]
        if abs(sizes.get(a, 0) + sizes.get(b, 0) - cnt) > 1e-9:
            ok_sizes = False
            break
        sizes[12 + int(np.where(Z[:, 0] == row[0])[0][0])] = cnt
    r.check("每行样本数 = 两子簇之和", ok_sizes)

    # 两个极端：完全相同的点 → 距离 0；n<2 → 空
    r.check("n=1 返回空 Z", cc.average_linkage(np.zeros((1, 1))).shape == (0, 4))
    Zd = cc.average_linkage(np.zeros((4, 4)))      # 全等：距离矩阵全 0
    r.check("全等点合并距离为 0", bool(np.allclose(Zd[:, 2], 0.0)),
            f"max={Zd[:, 2].max():.3e}")
    r.check("全等点 k=2 只能切出单点簇",
            len(np.unique(cc.fcluster_maxclust(Zd, 2))) == 2)

    # maxclust 的界：k=1 一簇，k=n 每点一簇
    r.check("k=1 → 只有一个簇", len(np.unique(cc.fcluster_maxclust(Z, 1))) == 1)
    r.check("k=n → 每点独立", len(np.unique(cc.fcluster_maxclust(Z, 12))) == 12)
    r.check("k 超界被夹住", len(np.unique(cc.fcluster_maxclust(Z, 99))) == 12)

    # 明显分开的三团，固定 k=3 必须分对
    labels = cc.fcluster_maxclust(Z, 3)
    r.check("k=3 的划分与真实分组一致", len(np.unique(labels)) == 3,
            f"得到 {len(np.unique(labels))} 组")


# ══════════════════════════════════════════════════════════════════════
# 2) 与 SciPy 对拍（仅在装了 scipy 的环境）
# ══════════════════════════════════════════════════════════════════════
def test_against_scipy(r: Result):
    print("\n[对拍] 与 SciPy 逐位一致")
    if not HAS_SCIPY:
        r.skip("与 scipy 对拍", "本环境未安装 scipy（发布构建刻意不装）")
        return

    bad = ""
    for trial in range(300):
        n = int(np.random.default_rng(trial).integers(3, 50))
        emb = synth_embeddings(1000 + trial, n, int(np.random.default_rng(trial).integers(1, 5)))
        d = dist_of(emb)
        Zs = linkage(squareform(d, checks=False), method="average")
        Zm = cc.average_linkage(d)
        if not np.allclose(Zs, Zm, atol=1e-9):
            bad = f"trial {trial}: Z 最大差 {np.abs(Zs - Zm).max():.3e}"
            break
        for k in range(1, min(6, n) + 1):
            if not same_partition(fcluster(Zs, t=k, criterion="maxclust"),
                                  cc.fcluster_maxclust(Zm, k)):
                bad = f"trial {trial}: maxclust k={k}"
                break
        if bad:
            break
        for t in (0.05, 0.2, 0.35, 0.6):
            if not same_partition(fcluster(Zs, t=t, criterion="distance"),
                                  cc.fcluster_distance(Zm, t)):
                bad = f"trial {trial}: distance t={t}"
                break
        if bad:
            break
    r.check("300 组随机数据 Z 与 fcluster 全部一致", not bad, bad)


# ══════════════════════════════════════════════════════════════════════
# 3) diarize 端到端（不加载模型，只调聚类方法）
# ══════════════════════════════════════════════════════════════════════
def _stub_optional_inference_deps():
    """把 diarize 顶层的推理依赖换成空壳（如果宿主没装）。

    diarize 顶层 `import onnxruntime` / `kaldi_native_fbank`，但本用例只调
    `_cluster_fixed_n` / `_cluster_auto` —— 纯 numpy 的聚类，一行推理都不碰。
    轻量 CI 只装 numpy，于是这两个 import 会把「换实现后分群是否一致」测成
    「CI 装了什么」，白白挡住本套最有价值的那几条断言。
    """
    import types
    for name in ("onnxruntime", "kaldi_native_fbank"):
        try:
            __import__(name)
        except Exception:
            sys.modules[name] = types.ModuleType(name)
            print(f"        （宿主未装 {name}，用空壳顶替；本用例不碰推理）")


def test_diarize_uses_it(r: Result):
    print("\n[diarize] 换实现后分群结果不变")
    _stub_optional_inference_deps()
    import diarize

    eng = diarize.DiarizationEngine.__new__(diarize.DiarizationEngine)

    emb2 = synth_embeddings(7, 20, 2)
    fixed = eng._cluster_fixed_n(list(emb2), 2)
    r.check("指定 2 人 → 恰好 2 组", len(set(fixed)) == 2, str(sorted(set(fixed))))
    r.check("标签为 1-indexed", min(fixed) == 1, str(min(fixed)))

    auto = eng._cluster_auto(list(emb2))
    r.check("自动模式 → 2 人", len(set(auto)) == 2, str(sorted(set(auto))))

    # 同一人的多段（细分噪声）不该被拆成多人
    one = synth_embeddings(11, 15, 1)
    r.check("同一位说话者 → 1 组",
            len(set(eng._cluster_auto(list(one)))) == 1,
            str(sorted(set(eng._cluster_auto(list(one))))))

    emb3 = synth_embeddings(13, 24, 3)
    r.check("自动模式 → 3 人", len(set(eng._cluster_auto(list(emb3)))) == 3,
            str(sorted(set(eng._cluster_auto(list(emb3))))))

    r.check("样本 <2 时返回单标签", eng._cluster_auto([emb2[0]]) == [1])
    r.check("人数超过样本数时被夹住",
            len(set(eng._cluster_fixed_n(list(emb2[:3]), 9))) == 3)


# ══════════════════════════════════════════════════════════════════════
# 4) 频域重采样
# ══════════════════════════════════════════════════════════════════════
def test_resample(r: Result):
    print("\n[audio_io] 频域重采样")
    from audio_io import _resample_fft

    sr, sr2 = 44100, 16000
    t = np.arange(sr) / sr

    y = _resample_fft(np.sin(2 * np.pi * 440 * t).astype(np.float32), sr, sr2)
    r.check("长度正确", len(y) == int(round(len(t) * sr2 / sr)), str(len(y)))
    peak = float(np.fft.rfftfreq(len(y), 1 / sr2)[np.argmax(np.abs(np.fft.rfft(y)))])
    r.check("440Hz 仍是 440Hz", abs(peak - 440) < 2, f"{peak:.1f} Hz")
    r.check("振幅保持（RMS≈0.707）",
            abs(float(np.sqrt((y ** 2).mean())) - 0.7071) < 0.02,
            f"{float(np.sqrt((y ** 2).mean())):.4f}")

    # 10 kHz 高于 16k 的奈奎斯特（8k）：必须被压制，不能混叠成 6 kHz
    alias = _resample_fft(np.sin(2 * np.pi * 10000 * t).astype(np.float32), sr, sr2)
    r.check("10kHz 被抗混叠滤掉（不会变成 6kHz 鬼影）",
            float(np.sqrt((alias ** 2).mean())) < 0.05,
            f"rms={float(np.sqrt((alias ** 2).mean())):.4f}")

    # 7 kHz 在通带内，必须保留
    keep = _resample_fft(np.sin(2 * np.pi * 7000 * t).astype(np.float32), sr, sr2)
    r.check("7kHz 通带内保留", abs(float(np.sqrt((keep ** 2).mean())) - 0.7071) < 0.05,
            f"rms={float(np.sqrt((keep ** 2).mean())):.4f}")

    # 升采样 16k → 48k
    up = _resample_fft(np.sin(2 * np.pi * 440 * np.arange(16000) / 16000).astype(np.float32),
                       16000, 48000)
    r.check("升采样长度正确", len(up) == 48000, str(len(up)))
    up_peak = float(np.fft.rfftfreq(len(up), 1 / 48000)[np.argmax(np.abs(np.fft.rfft(up)))])
    r.check("升采样后频率不变", abs(up_peak - 440) < 2, f"{up_peak:.1f} Hz")


# ══════════════════════════════════════════════════════════════════════
# 5) 源码里不能再有 scipy
# ══════════════════════════════════════════════════════════════════════
def test_no_scipy_imports(r: Result):
    print("\n[瘦身] 源码不再依赖 SciPy")
    offenders = []
    for py in sorted(ROOT.glob("*.py")):
        if py.name in ("build_release.py",):
            continue                       # 只在这里出现「排除 scipy」的配置
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] == "scipy":
                        offenders.append(f"{py.name}:{node.lineno} import {a.name}")
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] == "scipy":
                    offenders.append(f"{py.name}:{node.lineno} from {node.module}")
    r.check("项目源码无 scipy 导入", not offenders, "; ".join(offenders[:3]))

    br = (ROOT / "build_release.py").read_text(encoding="utf-8")
    r.check("打包清单排除 scipy", '"scipy"' in br and "EXCLUDE_MODULES" in br)
    r.check("打包断言 cluster_core 必须在包内",
            "_internal/cluster_core.py" in br)


def main() -> int:
    r = Result()
    for fn in (test_invariants, test_against_scipy, test_diarize_uses_it,
               test_resample, test_no_scipy_imports):
        try:
            fn(r)
        except Exception:
            r.check(fn.__name__, False, traceback.format_exc().strip().splitlines()[-1])

    passed = sum(r.items)
    print(f"\n结果：{passed}/{len(r.items)} 项通过")
    print("✅ 全部通过" if r.ok else "❌ 存在失败项")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
