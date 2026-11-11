"""cluster_core.py - 自带的层次聚类（average linkage），替掉 scipy。

为什么自己写
------------
说话人聚类只用到 SciPy 的 `linkage(method="average")` + `fcluster`，
而 SciPy 在 Windows 包里是 **69 MB**（scipy/ 49.7 + scipy.libs/ 19.3），
光 OpenBLAS 就 19 MB。为了三个函数背 69 MB，不划算。

这里实现的是同一套算法，语义与 SciPy 逐条对齐：

  * `average_linkage(dist)`  ←→ `linkage(squareform(dist), method="average")`
    Lance-Williams 更新：d(k, i∪j) = (n_i·d(k,i) + n_j·d(k,j)) / (n_i + n_j)
  * `fcluster_maxclust(Z, k)` ←→ `fcluster(Z, t=k, criterion="maxclust")`
    按合并顺序并簇，直到簇数 == k（Z 单调不减，与 SciPy 的阈值二分等价）
  * `fcluster_distance(Z, t)` ←→ `fcluster(Z, t=t, criterion="distance")`
    合并所有距离 ≤ t 的簇

标签编号规则也照抄 SciPy：按「簇内最小原始索引」升序编 1..k，
这样换实现不会让「说话者1」突然变成别人。

复杂度 O(M³)，M = 说话人窗口数（几十到几百），实测 300 窗 < 30 ms。
"""

from __future__ import annotations

import numpy as np

__all__ = ["average_linkage", "fcluster_maxclust", "fcluster_distance"]


def average_linkage(dist: np.ndarray) -> np.ndarray:
    """平均连接层次聚类。

    参数
    ----
    dist : (n, n) 对称距离矩阵，对角为 0。

    返回
    ----
    Z : (n-1, 4) float64，每行 [簇A, 簇B, 合并距离, 新簇样本数]，
        簇编号 < n 为原始点，≥ n 为该步产生的新簇（n + 行号）。
        n < 2 时返回空数组 (0, 4)。
    """
    d = np.asarray(dist, dtype=np.float64)
    n = d.shape[0]
    if n < 2:
        return np.zeros((0, 4), dtype=np.float64)

    # 只在该上三角里找最近对；合并后把被并掉的列设为 inf 使其退出竞争。
    work = d.copy()
    np.fill_diagonal(work, np.inf)
    sizes = np.ones(n, dtype=np.float64)
    # active[i] = 该槽位当前的簇编号；被合并者置 -1
    active = list(range(n))

    z = np.zeros((n - 1, 4), dtype=np.float64)
    for step in range(n - 1):
        # 最小距离位置（work 对称，取上三角）
        flat = np.argmin(work)
        i, j = divmod(int(flat), n)
        if i > j:                       # 保证 i < j，便于固定输出顺序
            i, j = j, i
        dij = float(work[i, j])

        new_id = n + step
        # SciPy 保证 Z 每行左小右大（按簇编号），标签顺序才稳定
        ca, cb = sorted((active[i], active[j]))
        z[step] = (ca, cb, dij, sizes[i] + sizes[j])

        # Lance-Williams 平均连接更新：对每个 k，用加权平均替换 (i, j)
        si, sj = sizes[i], sizes[j]
        w_i, w_j = si / (si + sj), sj / (si + sj)
        row = w_i * work[i, :] + w_j * work[j, :]
        work[i, :] = row
        work[:, i] = row
        work[i, i] = np.inf             # 自己与自己不竞争

        # j 退役
        work[j, :] = np.inf
        work[:, j] = np.inf
        sizes[i] = si + sj
        active[i] = new_id
        active[j] = -1

    return z


def _labels_from_z(z: np.ndarray, n: int, stop_at: int) -> np.ndarray:
    """按 Z 顺序并簇，簇数降到 stop_at 即停；返回 1-indexed 标签。

    Z 的隐含语义：第 step 行合并「簇 a」「簇 b」，产物编号为 n+step。
    a/b 可能是原始点（< n），也可能是前几行生成的新簇 —— 因此不能用
    纯 union-find（它不记录「新编号对应哪两个旧簇」），必须显式持有
    每个簇的成员表。
    """
    clusters: dict[int, list[int]] = {i: [i] for i in range(n)}
    for step in range(len(z)):
        if len(clusters) <= stop_at:
            break
        a, b = int(z[step, 0]), int(z[step, 1])
        merged = clusters.pop(a, []) + clusters.pop(b, [])
        clusters[n + step] = merged

    # 按「簇内最小原始索引」升序编号 → 与 SciPy 的标签顺序一致
    members = sorted(clusters.values(), key=min)
    labels = np.zeros(n, dtype=np.int64)
    for idx, group in enumerate(members, start=1):
        for i in group:
            labels[i] = idx
    return labels


def fcluster_maxclust(z: np.ndarray, k: int) -> np.ndarray:
    """把树切成（至多）k 个簇，等价于 fcluster(Z, t=k, criterion="maxclust")。"""
    n = int(z.shape[0]) + 1 if len(z) else 0
    if n == 0:
        return np.zeros(0, dtype=np.int64)
    k = max(1, min(int(k), n))
    return _labels_from_z(z, n, k)


def fcluster_distance(z: np.ndarray, t: float) -> np.ndarray:
    """合并所有距离 ≤ t 的簇，等价于 fcluster(Z, t=t, criterion="distance")。

    与 SciPy 一致：按 Z 顺序合并，遇到第一行距离 > t 即整体停止
    （不是「跳过该行继续合并更远的」—— 那样会跨越已存在的空隙）。
    """
    n = int(z.shape[0]) + 1 if len(z) else 0
    if n == 0:
        return np.zeros(0, dtype=np.int64)
    keep = 0
    for step in range(len(z)):
        if float(z[step, 2]) > float(t):
            break
        keep += 1
    return _labels_from_z(z[:keep], n, 1)
