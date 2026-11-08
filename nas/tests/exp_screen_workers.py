#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SCREEN_WORKERS 1/2/4 对照实验 —— **只读探针, 不参与决策**

为什么要有这个文件
------------------
`tests/replay.py` 的 2c 段只能回答「同一配置下分数怎么变」。要回答下面这两个问题, 样本里**攒不出来**
(生产每天只跑一个 W):

  ① 4 路并发是不是把每路读数压成了 1/4? 压掉的是「节点差」还是「出口总量」?
  ② 它改不改变粗筛**排名**? 也就是「谁进前 8」是不是并发度的函数 —— 若是, 粗筛这一关比假设的弱。
  ③ 「前 8」不稳(实测同一配置隔一轮只重合 3/8)到底**丢了什么**? `SCREEN_N=8` 该不该动?
     —— 这一条不靠再烧一遍带宽: 格子里存的是**全部 48 个节点**的分数, 换 N 只是换个切口。

所以只能排: 同一批节点、同一段时间窗里换并发跑。这是本项目唯一需要动并发口径的事, 单独立一个探针。

它**不写**任何交付状态: 不碰 results.json / samples.jsonl / 路由器 / 锁, 只打表 + 可选落一份 jsonl。
被测函数是主程序里那个 `_screen()` **本体**(不是复制品), 只是把模块级 `SCREEN_WORKERS` 临时改掉之后调用。

设计, 以及为什么只能这样
------------------------
- **节点集合固定**: L0 只跑一次, 取握手最快的前 N 个, 之后每一格都用这同一批。
  (P1-A 的教训: 两次跑若节点集合不同, 差值是**集合差**不是并发差。)
- **按轮交错**: 每轮把各档各跑一遍, 相邻轮把顺序整体反过来 —— 出口随时间的漂移被平均到三档上,
  而不是「先跑完 W=1 再跑 W=4」把漂移全算给 W。
- **预算用生产值**: 每格断言 `skip` 为空。一旦有节点没测到, 那一格与别格不可比, 直接标出来而不是静默平均。
- **wave 自证**: 每格记 `max(screen_wave)`, 它必须等于 W。否则「改 SCREEN_WORKERS」这件事本身
  没生效(比如 max_workers 被别处覆盖), 后面的数全是假的 —— 这正是 P1-A 栽过的地方。
- **合计吞吐必须用墙钟算**: `总字节 / 那一格耗时`。**不能**把各节点分数相加 —— 串行格(W=1)的 48 个
  读数分属 48 个不同瞬间, 相加会凭空造出一个从未同时存在过的总量(实测把 32 MiB/s 报成 2611)。
  这条是本探针第一版真踩过的错, 保留说明: 同一批字节、同一段时间里搬完多少, 才是可比的总量。
- **自洽检查**: 若每路下降只是「分摊 + 总量上涨」, 则 每路比值应 ≈ (合计涨幅 ÷ 并发路数)。
  对不上就说明还有第三个因素(排队/丢包/调度顺序), 不能一句「分摊」糊过去。

用法
----
    python3 tests/exp_screen_workers.py                          # Cn1, W=1/2/4, 12 轮, 前 48 个节点
    python3 tests/exp_screen_workers.py --pool Cn2 --rounds 15 --nodes 32
    python3 tests/exp_screen_workers.py --json /tmp/exp.jsonl     # 落原始格
    python3 tests/exp_screen_workers.py --from-json /tmp/exp.jsonl --screen-n 4,8,12,16
                                                                 # 只重出表, 不测速(第六节: 截断的代价)
    python3 tests/exp_screen_workers.py --screen-n ""            # 跳过第六节

退出码: 0 = 全部格完整可比; 1 = 有格被 skip/0 分污染(表照打, 但结论只能当参考)。
"""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load_mod(root):
    """按路径加载 `xbox-speedtest.py`(文件名带横线, 不能普通 import)。"""
    p = os.path.join(root, "xbox-speedtest.py")
    spec = importlib.util.spec_from_file_location("xst_exp", p)
    if spec is None or spec.loader is None:
        raise SystemExit("加载失败: %s" % p)
    m = importlib.util.module_from_spec(spec)
    sys.modules["xst_exp"] = m
    spec.loader.exec_module(m)
    return m


def pct(vals, q):
    """分位数(线性插值)。py3.8 的 statistics 没有 quantiles, 自己算一行。"""
    if not vals:
        return 0.0
    s = sorted(vals)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return float(s[lo] + (s[hi] - s[lo]) * (k - lo))


def med(vals):
    return float(statistics.median(vals)) if vals else 0.0


def spearman(a, b):
    """两个等长序列的秩相关(并列取平均秩)。用来回答「换并发, 排名还认得出是同一批节点吗」。"""
    n = len(a)
    if n < 3:
        return 0.0

    def ranks(v):
        idx = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[idx[j + 1]] == v[idx[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[idx[k]] = avg
            i = j + 1
        return r

    ra, rb = ranks(a), ranks(b)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    da = sum((ra[i] - ma) ** 2 for i in range(n)) ** 0.5
    db = sum((rb[i] - mb) ** 2 for i in range(n)) ** 0.5
    return (num / (da * db)) if da and db else 0.0


def _topk(d, k):
    """按分数降序取前 k 个 IP。并列时按 IP 字典序 —— 两处必须用同一把尺子, 否则「重合率」
    会变成「排序稳定性」的函数而不是数据的函数。"""
    return [ip for ip, _ in sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))[:k]]


def _rank_of(sc, ip):
    """ip 在**这一轮**分数里的名次(1 起)。不在这一轮里返回 0。"""
    for k, x in enumerate(_topk(sc, len(sc)), 1):
        if x == ip:
            return k
    return 0


def one_cell(mod, batch, host, path, ms_of, w):
    """跑一格: 真 `_screen()`, 只把模块级 SCREEN_WORKERS 换成 w。"""
    capped, skip, zero, wave = [], [], [], {}
    buf = io.StringIO()
    old = mod.SCREEN_WORKERS
    mod.SCREEN_WORKERS = w
    t0 = time.perf_counter()
    try:
        with contextlib.redirect_stdout(buf):
            ranked = mod._screen(batch, host, path, ms_of=ms_of, capped_out=capped,
                                 skip_out=skip, zero_out=zero, wave_out=wave)
    finally:
        mod.SCREEN_WORKERS = old
    el = time.perf_counter() - t0
    return {
        "w": w,
        "n_nodes": len(batch),
        "el": round(el, 2),
        "scores": dict((ip, float(sp)) for sp, ip in ranked),
        "capped": list(capped),
        "skip": list(skip),
        "zero": list(zero),
        "wave": dict(wave),
        "log": buf.getvalue(),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=ROOT, help="主程序所在目录(默认本文件上一级)")
    ap.add_argument("--pool", default="Cn1")
    ap.add_argument("--workers", default="1,2,4",
                    help="要对照的并发档位, 逗号分隔。默认 1,2,4 —— 生产值是 4")
    ap.add_argument("--rounds", type=int, default=12,
                    help="轮数(每轮把各档各跑一遍)。默认 12: 中位数要能压住单格抖动")
    ap.add_argument("--nodes", type=int, default=None,
                    help="固定节点数, 默认 = 主程序的 RTT_TOP(48), 与生产粗筛同形")
    ap.add_argument("--warmup", type=int, default=1,
                    help="丢弃的预热轮数(默认 1): 首次连接的 DNS/ARP/路由缓存不该算进任何一档")
    ap.add_argument("--json", default=None, help="把每一格的原始分数落成 jsonl")
    ap.add_argument("--from-json", default=None,
                    help="不测速, 直接对一份 --json 落盘的格重新出表(加分析/复核时用, 不必再烧一遍带宽)")
    ap.add_argument("--screen-n", default="4,8,12,16",
                    help="第六节要对照的 SCREEN_N 档位, 逗号分隔; 给空串则不跑那一节")
    ap.add_argument("--recall-w", type=int, default=None,
                    help="第六节用哪个并发档的格(默认最大档 = 生产档)")
    a = ap.parse_args()
    ns = [int(x) for x in a.screen_n.split(",") if x.strip()]

    if a.from_json:
        cells = [json.loads(l) for l in open(a.from_json, encoding="utf-8") if l.strip()]
        use = [c for c in cells if not c.get("warmup")]
        if not use:
            raise SystemExit("%s 里没有非预热格" % a.from_json)
        ws = sorted(set(int(c["w"]) for c in use))
        n_nodes = int(cells[0].get("n_nodes") or max(len(c["scores"]) for c in use))
        mod = load_mod(a.dir)          # 拿 SCREEN_CHUNK / SCREEN_N / MAX_QUICK_CANDS 这几个常量
        print("复核 %s: %d 格(其中预热 %d 已剔)" % (a.from_json, len(cells), len(cells) - len(use)))
        return 0 if report(use, ws, n_nodes, mod.SCREEN_CHUNK // 1048576, ns=ns,
                           prod_n=mod.SCREEN_N, max_cands=mod.MAX_QUICK_CANDS,
                           recall_w=a.recall_w) else 1

    mod = load_mod(a.dir)
    ws = [int(x) for x in a.workers.split(",") if x.strip()]
    if not ws:
        raise SystemExit("--workers 为空")
    n = a.nodes or mod.RTT_TOP

    pool = [p for p in mod.POOLS if p[0] == a.pool]
    if not pool:
        raise SystemExit("没有这个组: %s (有 %s)" % (a.pool, [p[0] for p in mod.POOLS]))
    name, path, host, url_path, _doms = pool[0]
    if not os.path.isfile(path):
        raise SystemExit("池文件不存在: %s" % path)
    rows = mod.load_ips(path)

    print("=" * 78)
    print("SCREEN_WORKERS 对照实验 · %s · 档位 %s · 每档 %d 轮(另丢 %d 轮预热) · 固定 %d 个节点"
          % (name, ws, a.rounds, a.warmup, n))
    print("  池 %s, 节点 %d 个; 目标 %s%s; 粗筛块 %d MiB, 每路撞顶线 %.0f MiB/s"
          % (os.path.basename(path), len(rows), host, url_path,
             mod.SCREEN_CHUNK // 1048576, mod.SPEED_CEIL_MIB))
    print("  生产并发 SCREEN_WORKERS = %d, 生产粗筛预算 %.0fs" % (mod.SCREEN_WORKERS, mod.SCREEN_BUDGET_SEC))
    print("-" * 78)

    # ---- L0 只跑一次: 之后每一格都用这同一批节点 ----
    t0 = time.perf_counter()
    stats = {}
    prox = mod.rtt_rank(rows, stats=stats)
    print("L0: 入选 %d/%d 个, 实发握手 %s, 通 %s, 用时 %.1fs"
          % (len(prox), len(rows), stats.get("probed"), stats.get("alive"),
             time.perf_counter() - t0))
    if not prox:
        raise SystemExit("L0 一个都不通, 实验无法成立(此时粗筛退回全池, 与生产不同形)")
    batch = [(ip, tag) for _ms, ip, tag in prox[:n]]
    ms_of = dict((ip, ms) for ms, ip, _t in prox)
    if len(batch) < n:
        print("! 只凑到 %d 个节点(< 请求的 %d): 池子里可握手的就这么多" % (len(batch), n))
    print("固定节点集(前 3): %s; 握手 中位 %.1fms / 最慢 %.1fms"
          % (", ".join("%s@%.1fms" % (ip, ms_of[ip]) for ip, _ in batch[:3]),
             med([ms_of[ip] for ip, _ in batch]), max(ms_of[ip] for ip, _ in batch)))

    cells = []
    t_all = time.perf_counter()
    for r in range(a.rounds + a.warmup):
        order = ws if r % 2 == 0 else list(reversed(ws))   # 交错 + 相邻轮反向, 抵消漂移
        line = []
        for w in order:
            c = one_cell(mod, batch, host, url_path, ms_of, w)
            c["round"] = r
            c["warmup"] = r < a.warmup
            cells.append(c)
            line.append("W=%d %4.1fs/%2d" % (w, c["el"], len(c["scores"])))
        tag = "预热(丢)" if r < a.warmup else "第 %2d/%d 轮" % (r - a.warmup + 1, a.rounds)
        print("  %s: %s" % (tag, " | ".join(line)), flush=True)

    use = [c for c in cells if not c["warmup"]]
    print("-" * 78)
    print("跑完 %d 格, 用时 %.1fs" % (len(use), time.perf_counter() - t_all))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            for c in cells:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        print("原始格已落盘: %s (%d 行)" % (a.json, len(cells)))
    return 0 if report(use, ws, len(batch), mod.SCREEN_CHUNK // 1048576, ns=ns,
                       prod_n=mod.SCREEN_N, max_cands=mod.MAX_QUICK_CANDS,
                       recall_w=a.recall_w) else 1


def sec_recall(use, ws, ns, prod_n, max_cands, w=None):
    """第六节: `SCREEN_N` 该取几 —— 把「前 8 不稳」换算成**截断的代价**。

    为什么不用「前 8 重合率」当判据
    ------------------------------
    重合率只说明两次测量像不像, 不说明**丢了什么**。第 8 名与第 9 名只差 0.3% 时, 换人对
    决策毫无影响; 差 20% 才是真丢。所以这一节给三个带尺度的量:

      · **含真冠军**: 这一轮 TopN 里有没有那个「剔掉本轮、其余轮中位最高」的节点。
      · **后悔**(regret): 按**本轮**分数在 TopN 里挑最好的那个, 它比真冠军慢多少(%)。
        这是整份报告里唯一带单位的损失 —— 名次全变而后悔≈0, 就等于没损失。
      · **边界差**: 第 N 与第 N+1 名的相对差。它解释后悔为什么大或小。

    真值一律 **剔掉被评的那一轮**(leave-one-out): 中位里若含本轮, 就是拿答案去对答案 ——
    而「同一批分数」既是候选又是裁判, 会把召回率系统性地抬高。

    代价也不是线性的: 候选 = Top{N} + 本地保送 + 当前 IP, 再截到 `MAX_QUICK_CANDS`。
    N 调大不是「多测几个」, 而是把保送/当前的位置挤掉; N ≥ 那个硬顶在结构上无效。
    """
    w = w or ws[-1]
    rs = [c for c in use if c["w"] == w]
    if len(rs) < 3:
        print("\n六、SCREEN_N: 该档只有 %d 轮(< 3), 真值不可靠, 不做召回分析" % len(rs))
        return
    n_r = len(rs)
    truth = []
    for i in range(n_r):
        per = {}
        for j, c in enumerate(rs):
            if j == i:
                continue
            for ip, sp in c["scores"].items():
                per.setdefault(ip, []).append(sp)
        truth.append(dict((ip, med(v)) for ip, v in per.items()))

    print("\n六、SCREEN_N 该取几 —— 用同一批格算「截断的代价」(档位 W=%d, %d 轮)" % (w, n_r))
    print("  真值 = 剔掉本轮、其余 %d 轮的逐 IP 中位; 生产 N=%d, 快测硬顶 MAX_QUICK_CANDS=%d"
          % (n_r - 1, prod_n, max_cands))
    print("  %-3s %-8s %-10s %-10s %-18s %-16s %-9s"
          % ("N", "含真冠军", "真前3命中", "真前8命中", "真冠军名次 P50/P90/最差",
             "后悔 P50/P90/最差", "边界差中位"))
    verdict = []
    missed = {}
    for n in ns:
        hit1, hit3, hit8 = 0, 0.0, 0.0
        ranks, regs, gaps = [], [], []
        miss_r = []
        for i, c in enumerate(rs):
            sc = c["scores"]
            tr = truth[i]
            tn = _topk(sc, n)
            if not tn or not tr:
                continue
            c_true = _topk(tr, 1)[0]
            pk = _rank_of(sc, c_true)
            if pk:
                ranks.append(pk)
            if c_true in tn:
                hit1 += 1
            else:
                miss_r.append((c["round"], pk))
            hit3 += len(set(tn) & set(_topk(tr, 3))) / 3.0
            hit8 += len(set(tn) & set(_topk(tr, 8))) / 8.0
            if tr.get(c_true):
                regs.append(1.0 - max(tr.get(ip, 0.0) for ip in tn) / tr[c_true])
            rk = _topk(sc, len(sc))
            if len(rk) > n:
                s1, s2 = sc[rk[n - 1]], sc[rk[n]]
                if s1:
                    gaps.append((s1 - s2) / s1)
        rg = ("%.0f/%.0f/%d" % (med(ranks), pct(ranks, 0.90), max(ranks))) if ranks else "-"
        rr = ("%.1f/%.1f/%.1f%%" % (med(regs) * 100.0, pct(regs, 0.90) * 100.0,
                                     max(regs) * 100.0)) if regs else "-"
        print("  %-3d %5d/%-2d %7.2f/3  %7.2f/8  %-18s %-16s %8.1f%%"
              % (n, hit1, n_r, hit3 / n_r, hit8 / n_r, rg, rr, med(gaps) * 100.0))
        verdict.append((n, hit1, n_r, med(regs) * 100.0,
                        sum(1 for x in regs if x > 0.05), len(regs)))
        missed[n] = miss_r
    print("  读法: 「后悔」是唯一带速度单位的列 —— 名次全变而它≈0, 就是没损失;")
    print("        「边界差」小(几 %)说明第 N 与 N+1 本来就是一个水平线, 换谁是噪声。")
    print("  代价不是线性的: 候选 = Top{N} + 本地保送 + 当前 IP 再截到 MAX_QUICK_CANDS=%d ——" % max_cands)
    print("        N 调大不是多测几个, 是把保送/当前的位置挤掉; N ≥ %d 在结构上无效。" % max_cands)
    for n, h1, nr, reg, bad, tot in verdict:
        tag = "  ← 生产值" if n == prod_n else ""
        print("  N=%-2d: 真冠军进 Top%d %d/%d 轮; 后悔>5%% 的轮次 %d/%d%s"
              % (n, n, h1, nr, bad, tot, tag))
        if missed.get(n):
            print("        丢在: %s" % ", ".join("r%d(真冠军本轮第 %d 名)" % (r, p)
                                              for r, p in missed[n]))
    # 一句话结论: 用**观测到的最差名次**定下 N 的下界, 并把生产值的实际损失报成一个数
    prod = [v for v in verdict if v[0] == prod_n]
    all_ranks = []
    for i, c in enumerate(rs):
        pk = _rank_of(c["scores"], _topk(truth[i], 1)[0]) if truth[i] else 0
        if pk:
            all_ranks.append(pk)
    _prod_worst = 0.0
    if prod:
        _n, _h1, _nr, _reg, _bad, _tot = prod[0]
        for i, c in enumerate(rs):
            tr = truth[i]
            c_true = _topk(tr, 1)[0] if tr else None
            if not c_true or not tr.get(c_true):
                continue
            tn = _topk(c["scores"], _n)
            if tn:
                _prod_worst = max(_prod_worst, 1.0 - max(tr.get(ip, 0.0) for ip in tn) / tr[c_true])
    if all_ranks:
        need = max(all_ranks) + 1
        if need > max_cands:
            # 名次尾部越过候选硬顶 ⇒ 这一档的「最差名次」不能拿来定 N, 否则会推出
            # 「N ≥ 38」这种结构上做不到的数(NAS 上 W=1 那遍实测就是这样)。
            # 这时该看的是「后悔」: 名次乱但后悔小, 说明整批挤在一个水平线上, 换谁都一样。
            print("  一句话: 真冠军本轮名次 P50=%.0f / P90=%.0f / 最差=%d —— 尾部已越过候选硬顶 %d,"
                  " **这一档的名次尾部不可信**, 不能按最大名次定 N(硬要覆盖得 N≥%d);"
                  % (med(all_ranks), pct(all_ranks, 0.90), max(all_ranks), max_cands, need))
            print("          代价要看后悔: 生产 N=%d 最大 %.1f%%(名次乱而代价小 ⇒ 整批在一个水平线上)。"
                  % (prod_n, _prod_worst * 100.0))
        else:
            print("  一句话: 真冠军的本轮名次 P50=%.0f / P90=%.0f / 最差=%d ⇒ **N ≥ %d** 才能把观测到的最差情况圈进来;"
                  % (med(all_ranks), pct(all_ranks, 0.90), max(all_ranks), need))
            if prod:
                print("  生产 N=%d 的实际账: 丢真冠军 %d/%d 轮, 最差一轮慢 %.1f%%(以 8MiB 中位为真值)。"
                      % (prod[0][0], prod[0][2] - prod[0][1], prod[0][2], _prod_worst * 100.0))


def report(use, ws, n_nodes, chunk_mib, ns=None, prod_n=None, max_cands=None, recall_w=None):
    """把若干格 → 一张表。**只吃数据**, 不碰网络 —— 所以它也能直接跑一份 jsonl(`--from-json`)。"""
    # 完整性: 任何一格被 skip/0 分污染, 它就和别格不可比
    bad = [c for c in use if c["skip"] or c["zero"] or len(c["scores"]) != n_nodes]
    print("完整性: %d/%d 格测满 %d 个节点%s"
          % (len(use) - len(bad), len(use), n_nodes,
             "" if not bad else " —— **%d 格不完整**: %s"
             % (len(bad), ", ".join("r%d/W%d(skip %d, zero %d, 有分 %d)"
                                    % (c["round"], c["w"], len(c["skip"]), len(c["zero"]),
                                       len(c["scores"])) for c in bad[:6]))))
    # wave 自证: 改了 SCREEN_WORKERS 到底生没生效
    for w in ws:
        got = sorted(set(max(c["wave"].values()) for c in use if c["w"] == w and c["wave"]))
        print("  W=%d 自证: max(screen_wave) 实际取值 %s %s"
              % (w, got, "OK" if got == [w] else "**与配置不符 ⇒ 这一档的并发没生效**"))

    # 配对顺序固定成"IP 排序", 不按分数 —— 否则秩相关会被排序本身污染。
    # 不用原 batch 序: `--from-json` 时没有它, 而两处必须是同一把尺子。
    seen = []
    for c in use:
        for ip in c["scores"]:
            if ip not in seen:
                seen.append(ip)
    order = sorted(seen)

    # ---- 逐 IP 中位: 每档一张 {ip: 中位} ----
    by_w = {}
    for w in ws:
        per_ip = {}
        for c in use:
            if c["w"] != w:
                continue
            for ip, sp in c["scores"].items():
                per_ip.setdefault(ip, []).append(sp)
        by_w[w] = dict((ip, med(v)) for ip, v in per_ip.items())
    base = ws[0]        # 对照基准: 最小并发档(不是生产的 4) —— 问的是"加并发改变了什么"

    print("\n" + "=" * 78)
    print("一、每档水平(逐 IP 取各轮中位, 再对 IP 取中位)")
    print("  档位   每路中位   每路 P10/P90   撞顶格/总格   单格耗时中位")
    for w in ws:
        vals = list(by_w[w].values())
        capn = sum(1 for c in use if c["w"] == w and c["capped"])
        n_w = len([c for c in use if c["w"] == w])
        print("  W=%-2d  %7.1f   %5.1f /%5.1f  %9d/%-3d   %7.2fs"
              % (w, med(vals), pct(vals, 0.10), pct(vals, 0.90), capn, n_w,
                 med([c["el"] for c in use if c["w"] == w])))

    print("\n二、并发换来了多少总量(同一批 %d×%dMiB, 只看**墙钟**)" % (n_nodes, chunk_mib))
    # **不能用 sum(每节点分数) 当吞吐**。W=1 时 48 个读数发生在**不同瞬间**(串行), 相加等于凭空
    # 造一个从未同时存在过的总量: 实测把 32 MiB/s 的链路报成 2611 MiB/s。吞吐只能由「总字节 / 墙钟」
    # 得到 —— 那一格确实在这段时间里搬完了这么多字节, 与并发度无关地成立。
    el = dict((w, med([c["el"] for c in use if c["w"] == w])) for w in ws)
    byt = n_nodes * float(chunk_mib)
    agg = dict((w, (byt / el[w]) if el[w] else 0.0) for w in ws)
    for w in ws:
        print("  W=%-2d 单格中位 %5.2fs ⇒ 合计 %6.1f MiB/s (%+.0f%% vs W=%d)"
              % (w, el[w], agg[w],
                 (agg[w] / agg[base] - 1.0) * 100.0 if agg[base] else 0.0, base))
    print("  读法: 合计**明显上涨** ⇒ 单路跑不满, 并发是真收益, 每路下降是分摊不是损失;")
    print("        合计**不涨** ⇒ 出口已满, N 路只是把同一根管子切成 N 份,")
    print("        每路分数随并发下降 —— 排名量的就不是『节点多快』而是『它抢到几份』。")
    # 自洽: 若每路下降只是「分摊 + 总量上涨」, 那么 每路比值 ≈ (合计涨幅 / 并发路数)。
    # 对不上就说明还有第三个因素(排队、丢包、调度顺序), 不能一句"分摊"糊过去。
    for w in ws[1:]:
        rats2 = [by_w[w][ip] / by_w[base][ip] for ip in order
                 if by_w[base].get(ip) and by_w[w].get(ip)]
        pred2 = (agg[w] / agg[base]) / float(w)
        print("  W=%d 自洽: 合计涨 %.2f× ÷ %d 路 ⇒ 每路应 ×%.3f, 实测中位 ×%.3f%s"
              % (w, agg[w] / agg[base], w, pred2, med(rats2),
                 "" if abs(med(rats2) - pred2) < 0.10 else "  ← 差得远, 还有第三个因素"))

    def _tops(d, k=8):
        # 与 `_topk` 同一把尺子(并列按 IP 字典序), 只是返回集合 —— 两处若各写一份排序,
        # 并列处的差异会让「重合率」变成排序实现的函数。
        return set(_topk(d, k))

    print("\n三、排名还认得出吗 —— **先给噪声地板, 再看跨档位**")
    # 同一档位**不同轮**之间: 并发没变, 只剩网络抖动与进场顺序。这就是噪声地板 ——
    # 「换并发」造成的名次变化必须**跨过它**才算数。没有这条对照, 跨档位的低相关只能说明
    # 「两次测量不像」, 不能说明「并发改了名次」—— 而这两句话的处置完全不同。
    floor = {}
    for w in ws:
        rs = [c["scores"] for c in use if c["w"] == w]
        ovs, sps = [], []
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                com = [ip for ip in order if ip in rs[i] and ip in rs[j]]
                if len(com) < 3:
                    continue
                sps.append(spearman([rs[i][x] for x in com], [rs[j][x] for x in com]))
                ovs.append(len(_tops(rs[i]) & _tops(rs[j])))
        floor[w] = (med(sps), med(ovs))
        print("  W=%d 轮间(噪声地板, %d 对): Spearman 中位 %+.3f · 前 8 重合中位 %.0f/8"
              % (w, len(sps), med(sps), med(ovs)))
    for i, w1 in enumerate(ws):
        for w2 in ws[i + 1:]:
            ips = [ip for ip in order if ip in by_w[w1] and ip in by_w[w2]]
            # 变量名避开 `a`(argparse 的 Namespace) —— 上一版在这里遮蔽了它, 于是结尾
            # `if a.json:` 抛 AttributeError: 'list' object has no attribute 'json',
            # 整轮原始格白跑。命名冲突在脚本里就是数据丢失。
            _va = [by_w[w1][ip] for ip in ips]
            _vb = [by_w[w2][ip] for ip in ips]
            ov = len(_tops(by_w[w1]) & _tops(by_w[w2]))
            fl = max(floor[w1][1], floor[w2][1])
            if ov < fl - 1:
                mark = "  ← 比地板低 %d 个: **超出噪声, 并发真的在改名次**" % (fl - ov)
            elif ov <= fl:
                mark = "  ← 与地板同量级: 名次差可由噪声解释, 不能归给并发"
            else:
                mark = ""
            print("  W=%d vs W=%d: Spearman %+.3f (n=%d) · 前 8 重合 %d/8  [地板 %.0f/8]%s"
                  % (w1, w2, spearman(_va, _vb), len(ips), ov, fl, mark))
    print("  读法: 跨档位重合率**没有明显低于**同档轮间波动 ⇒ 名次变化在噪声范围内, 不能归给并发;")
    print("        明显更低 ⇒ 并发真的在改名次, 粗筛这一关的排名依赖并发度。")

    print("\n四、逐 IP(按 W=%d 中位降序前 12)" % base)
    print("  %-16s" % "IP" + "".join("   W=%-2d" % w for w in ws) + "    相对 W=%d" % base)
    for ip, v0 in sorted(by_w[base].items(), key=lambda kv: -kv[1])[:12]:
        rel = ""
        if v0:
            rel = "  ".join("%+.0f%%" % ((by_w[w].get(ip, 0.0) / v0 - 1.0) * 100.0)
                            for w in ws if w != base)
        print("  %-16s" % ip + "".join("%7.1f" % by_w[w].get(ip, 0.0) for w in ws) + "   " + rel)

    # 逐 IP 的 W_hi/W_base 比值分布 —— 「精确除以 N」若成立, 中位比值就该接近 0.25
    if len(ws) >= 2:
        hi = ws[-1]
        rats = [by_w[hi][ip] / by_w[base][ip] for ip in order
                if by_w[base].get(ip) and by_w[hi].get(ip)]
        if rats:
            print("\n五、每路比值 W=%d / W=%d: 中位 %.3f (P10 %.3f / P90 %.3f), n=%d"
                  % (hi, base, med(rats), pct(rats, 0.10), pct(rats, 0.90), len(rats)))
            print("  0.25 = 精确分摊(每路拿到 1/N); 1.00 = 并发没有代价。实测中位 = %.3f" % med(rats))

    # 第六节只在调用方给了 N 清单时打(生产路径也走它, 默认 4,8,12,16)
    if ns:
        sec_recall(use, ws, ns, prod_n or 8, max_cands or 16, w=recall_w)

    print("=" * 78)
    return not bad


if __name__ == "__main__":
    sys.exit(main())
