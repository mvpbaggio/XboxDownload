#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
samples.jsonl 的**离线回放器** (v2.3.31, 审查方 P2-5)。

它回答的问题只有一个, 而且必须先于"攒够 30 天"回答:

    `WIN_RATIO` 从 1.30 改到 1.15, 在过去这些轮里会多换几次? 每次换来多少 MiB/s?

为什么这条必须先写: `samples.jsonl` 从 v2.3.28 起每轮每组 append 一行, 但**全仓没有一个
reader**。回放要回答上面那句话, 需要 `st_cur`(当前 IP 稳态)、`st_cand`(冠军稳态)、`wr`(当时
的门槛)三个数 —— 它们在 v2.3.30 之前根本不在样本里, 只能靠正则去抠 `kept (steady 103.6 <
cur 87.9*1.3)` 这种散文, 而切换分支的文案里一个数都没有。攒 30 天再发现"答不了", 那 30 天
的行是补不回来的。所以先写 60 行, 用它反证字段够不够。

**它不参与任何决策**: 决策路径一次都不读 samples.jsonl(A24 钉着), 这个文件只读不写、只打印。
它也不改 results.json / speed.log / .last_retest —— 想验"某个阈值会怎样", 就把输出当假设看,
不要拿它去写回任何落盘文件。

用法:
    python3 replay.py                    # 默认用 1.15 对比现行门槛(见 DEFAULT_RATIO)
    python3 replay.py 1.10              # 换成 1.10
    python3 replay.py 1.15 /path/samples.jsonl

判据(刻意简单, 与 main 的决策**同构但不同源**):
    换 = st_cand > st_cur * ratio       (两边都得是 >0 的有效稳态测量)
所以它算的是"迟滞这一条规则"的敏感性, 不含 REPLACE_MIN / 运营商分散 / 当前 IP 判死这些分支 ——
那些要读 results.json, 而那是结论不是分布。看不懂的地方宁可少算, 不要算错方向。

v2.3.32(审查方 P3-F): 无参数运行时原来 `new_ratio = None`, 于是翻转恒为 0(拿旧门槛比自己),
而 docstring 写着"默认用 1.15 对比现行门槛" —— **提示是对的, 默认是错的**: 不带参数跑一遍,
它不回答自己存在的那个问题, 只打一张字段完整率。现在默认值就是 DEFAULT_RATIO。
另外 `WIN_RATIO` 里 Cn1 的现行值恰好也是 1.15, 与最常输的假设值撞号 —— 那种情况下
"翻转 0 轮"不是"Cn1 不受影响", 而是"1.15 比 1.15", 所以下面会**明说**这句。

v2.3.33: `screen_capped` 这一列的空**不再是一句话**。当时以为"撞顶线随并发摊薄"(4 路 => 20MiB/s),
于是记 `screen_cap = 80/max(wave)`。

v2.3.34: **上面那句是错的, 已推翻**。撞顶线恒 = 80MiB/s(每路, 8MiB/0.1s), **与并发无关**;
并发改变的只是"够不够得着"它 —— 4 路满编时要有一路跑到 80, 出口总带宽得有 80×4 = 320MiB/s。
`80/max(wave)` 算的是"出口被均分时每路分到多少", 只有当出口被打满时才等于观察值。
真机自证(2026-11-08 04:00, Cn1): 48 个分数里 **41 个 > 20.0** 而 `screen_capped` 是空的 ——
一个分数不可能超过自己的撞顶线, 所以 20.0 从来不是那条线。
现在样本里是两个量: `screen_floor`(=80.0, 恒) 与 `screen_need`(=80×max(wave), 达到它所需出口)。
判读方法: `max(screen) × max(wave)` 是出口下界, 小于 `screen_need` => 撞不到是**结构性的**。
旧样本只有 `screen_cap`: 那一列按旧口径确实违反"分数 <= 上限", 不要拿它读任何东西。
"""
import json
import os
import statistics
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 现行门槛(与 xbox-speedtest.py 的 WIN_RATIO 同值)。这里抄一份是**故意的**: 回放器要能回答
# "如果当时是 1.15 会怎样", 就不能 import 主程序去拿"现在的值" —— 那会让答案跟着未来一起变。
# 它们是**现行值**这一侧, 假设值只从 argv 来(别把假设写进这个表, 否则"现行值变了吗"就看不出来了)。
WIN_RATIO = {"Cn1": 1.15, "Cn2": 1.30}

# 无参数时用的假设值。与 docstring 保持一致 —— 默认值本身就是"最常被问的那个问题"
# (Cn1 从 1.30 改到 1.15 会多换几次), 不是"什么都不比"。
DEFAULT_RATIO = 1.15

# 一份"够回放"的样本必须有的键。缺哪个就点名 —— 这就是这一轮补 screen_capped / quick_skip 的理由:
# 没有前者, `screen` 里的 80.0 到底是"测出 80"还是"至少 80"分不清; 没有后者,
# quick 里缺席的键到底是"预算跳过"还是"测了是 0"分不清。
# v2.3.32 补 screen_skip / screen_zero / screen_wave: 前两个是 quick_skip 在粗筛侧的对称
# (审查方 P1-B: 同一个论证写两遍, 漏了粗筛那一遍), 第三个是分数"被几路平分过"的唯一凭证。
# v2.3.33 补 screen_cap —— 那一版的口径是错的(80/max(wave)), v2.3.34 已拆成下面两个:
#   screen_floor 撞顶线本身(80.0, 与并发无关)  screen_need 达到它所需的出口总带宽(80×wave)
# 旧样本只有 screen_cap: 保留读取能力(判读那一段会点名叫你别用), 但它**不在 WANTED 里**了 ——
# 新样本不该再出现这个名字, 否则说明拆得不干净。
# v2.3.34 补 t_screen / t_quick: 原来三段耗时只由 `[t]` 那行打给人看, 落盘前被 pop 掉,
# 3405s 一轮事后算不出"哪一段在变慢"; 而 t_l0 写进每一行却从没被正文读过。
WANTED = ("ts", "group", "l0", "l0_tail", "l0_pool", "l0_probed", "l0_alive",
          "screen", "screen_capped", "screen_skip", "screen_zero", "screen_wave",
          "screen_floor", "screen_need",
          "quick", "quick_skip", "t_l0", "t_screen", "t_quick",
          "cur", "wr", "st_cur", "st_cand", "q_cur", "ip", "speed", "decision")


def load(path):
    rows, bad = [], 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                bad += 1
    return rows, bad


def _num(v):
    return v if isinstance(v, (int, float)) and v > 0 else None


def main(argv):
    new_ratio = DEFAULT_RATIO
    path = os.path.join(BASE, "samples.jsonl")
    if len(argv) > 1:
        try:
            new_ratio = float(argv[1])
        except ValueError:
            print("参数 1 要是数字(新门槛), 例如 1.15")
            return 2
    if len(argv) > 2:
        path = argv[2]
    if not os.path.exists(path):
        print("没有 %s: 还没有样本(或路径不对)。回放器在**攒到样本之前**就该存在, "
              "它证明的正是字段够不够。" % path)
        return 2
    rows, bad = load(path)
    print("样本 %d 行(坏行 %d), 来源 %s" % (len(rows), bad, path))
    if not rows:
        return 0

    # ---- 1. 字段完整率: 缺什么, 缺多少 ----
    print("\n[字段] 每行必须有的键:")
    for k in WANTED:
        miss = sum(1 for r in rows if k not in r or r.get(k) is None)
        flag = "ok" if miss == 0 else "缺 %d 行" % miss
        print("  %-14s %s" % (k, flag))

    # ---- 2. screen 撞顶比例: 80.0 是"测出来"还是"至少" ----
    capped_rows = sum(1 for r in rows if r.get("screen_capped"))
    # v2.3.34: 撞顶线恒 80(每路), 与并发无关。并发只决定"够不够得着": 4 路要 320MiB/s 出口。
    # 判据用**观测值反推** —— max(分数)×wave 是出口下界, 小于 need 就撞不到。这里没有"均分"假设:
    # 均分只在出口被打满时成立, 而真机实测 max(分数) = 73.2 远超 80/4 = 20, 恰恰证明没打满。
    floors = [r.get("screen_floor") for r in rows if r.get("screen_floor")]
    needs = [r.get("screen_need") for r in rows if r.get("screen_need")]
    caps_old = [r.get("screen_cap") for r in rows if r.get("screen_cap")]
    print("\n[screen] %d/%d 行有撞顶节点(值只是下界, 名次由 L0 握手时间决定)" % (capped_rows, len(rows)))
    if capped_rows == 0:
        if floors:
            print("[screen] 撞顶线 %s MiB/s(每路, 与并发无关); 达到它所需出口 %s MiB/s"
                  % (sorted(set(floors)), sorted(set(needs)) or "?"))
            _shown = 0
            for _r in rows:
                _fl, _nd = _r.get("screen_floor"), _r.get("screen_need")
                _sc, _wv = _r.get("screen") or [], _r.get("screen_wave") or {}
                _w = [v for v in _wv.values() if isinstance(v, int)]
                if not (_fl and _nd and _sc and _w):
                    continue
                _wmax, _mx = max(_w), max(float(x[0]) for x in _sc)
                _rev = _mx * _wmax
                if _rev < _nd:
                    print("[screen] %s %s: 最高单路 %.1f × %d 路 => 出口至少 %.0f < need %.0f "
                          "⇒ 撞不到是**结构性**的, 与'真没人撞'无关"
                          % (_r.get("ts"), _r.get("group"), _mx, _wmax, _rev, _nd))
                else:
                    print("[screen] %s %s: 最高单路 %.1f × %d 路 => 出口至少 %.0f >= need %.0f "
                          "⇒ 本该出现 capped 却没有, 先去查计时器而不是节点"
                          % (_r.get("ts"), _r.get("group"), _mx, _wmax, _rev, _nd))
                _shown += 1
                if _shown >= 8:      # 一行一组, 别把 30 天的样本全打出来
                    break
        elif caps_old:
            print("[screen] 撞顶 0 行, 样本带的是 **screen_cap=%s** —— 那是 v2.3.33 的错误口径"
                  "(80/并发), 别拿它读任何东西: 真机 41/48 个分数都超过了它。重新跑一轮才有 floor/need"
                  % sorted(set(caps_old)))
        else:
            print("[screen] 撞顶 0 行且样本没有任何撞顶线字段 —— 无法区分「这一档撞不到」与「真没人撞」"
                  "(v2.3.32 及更早的样本就是这个状态)")
    skipped = sum(len(r.get("quick_skip") or []) for r in rows)
    print("[quick] 累计预算跳过 %d 个候选(这些在 quick 里根本没有键, 与'测了是 0'不同)" % skipped)
    s_skip = sum(len(r.get("screen_skip") or []) for r in rows)
    s_zero = sum(len(r.get("screen_zero") or []) for r in rows)
    print("[screen] 累计没测到 %d 个 / 测了 0 分 %d 个(两者在 screen 里同样缺席, 含义相反)"
          % (s_skip, s_zero))
    waves = [v for r in rows for v in (r.get("screen_wave") or {}).values() if isinstance(v, int)]
    if waves:
        # v2.3.34: 不再说"被几路平分"。同节点跨轮分数会变, 但原因**未必是**分摊 —— 只有当
        # 出口被打满时才平分。这一列只是"进场那一刻几路在跑", 用于归因而非折算。
        print("[screen] 并发档位 %s(进场时刻同批请求数; 分数要跨档位比, 请先看下面的出口下界)"
              % dict((w, waves.count(w)) for w in sorted(set(waves))))

    # ---- 2c. 分数 vs 并发档位(观测, 不判定) ----
    # v2.3.36 (审查方 M1): v2.3.35 我在三处写了"逐 IP 的 wave 没入库", 那是**事实错误** ——
    # `screen_wave` 一直是 {ip: 进场时同批几路} 逐 IP 落盘的(build_sample), 上面 143 行本来就
    # 把它和 `screen` 配对取出来了(只是只拿了 max 去算出口下界, 没算逐 IP)。
    # 真正算不出相关性的原因是**单行内 wave 常无方差**: 满编时 48 个 IP 的 wave 全是 4, 相关
    # 系数必然除零; 必须跨行/跨档位攒。这两句话方向相反 ——
    #   "没入库"     = 结构上永远不可能 -> 账上"4 路分摊"会被判成不可测量
    #   "要攒档位"   = 这两周正好在收 -> 档位一多就能直接读出"同 IP 跨档位分数变不变"
    # 所以这里按 wave 分档打中位数/极差, 并对"出现在 >=2 个档位的 IP"做同 IP 对照。
    _pw = {}
    _by_ip = {}
    for _r in rows:
        _wv = _r.get("screen_wave") or {}
        for _sp, _ip in (_r.get("screen") or []):
            _w = _wv.get(_ip)
            if not isinstance(_w, int) or _w <= 0:
                continue
            _pw.setdefault(_w, []).append(float(_sp))
            _by_ip.setdefault(_ip, {}).setdefault(_w, []).append(float(_sp))
    if _pw:
        def _med(v):
            v = sorted(v)
            return v[len(v) // 2] if len(v) % 2 else (v[len(v) // 2 - 1] + v[len(v) // 2]) / 2.0
        print("\n[obs] 分数 vs 并发档位(跨 %d 行累积, 单行内 wave 常恒为满编所以必须跨行攒):" % len(rows))
        for _w in sorted(_pw):
            _v = sorted(_pw[_w])
            print("       wave=%d  n=%-4d 中位 %5.1f  极差 %.1f~%.1f"
                  % (_w, len(_v), _med(_v), _v[0], _v[-1]))
        _multi = [(ip, d) for ip, d in _by_ip.items() if len(d) >= 2]
        if _multi:
            print("       同 IP 跨档位(直接对照『同节点分数变不变』, 前 8 个):")
            for ip, d in sorted(_multi)[:8]:
                print("         %-15s " % ip + "  ".join(
                    "wave=%d 中位%5.1f(n=%d)" % (_w, _med(v), len(v)) for _w, v in sorted(d.items())))
        else:
            print("       还没有 IP 出现在 2 个以上档位 —— 这就是『要攒』: 攒到才有同 IP 对照，"
                  "现在只能看档位间中位数的整体移动(含节点构成差异, 不能单独归因到并发)")
    else:
        print("\n[obs] 样本里没有 screen_wave 能与 screen 配对的行 —— 这一列先攒数据")

    # ---- 2b. 分段耗时: 三段 P50/P90 ----
    # v2.3.34 (审查方 P2-E): t_screen / t_quick 原来在落盘前被 pop, 3405s 一轮事后算不出
    # "哪一段在变慢"; 而 t_l0 写进每一行、正文从没读过。三段一起看才有意义: L0 涨=池子长了,
    # 粗筛涨=节点多/带宽挤, 快测涨=候选多或单节点慢。
    _seg = (("t_l0", "L0 握手"), ("t_screen", "粗筛"), ("t_quick", "快测"))
    _seg_have = False
    for _k, _label in _seg:
        _v = sorted(float(r[_k]) for r in rows if _num(r.get(_k)))
        if not _v:
            continue
        if not _seg_have:
            print("\n[耗时] 分段(秒); **只比同一侧的历史, 换机器后不可直接比**")
            _seg_have = True
        _p90 = _v[min(len(_v) - 1, int(len(_v) * 0.9))]
        print("  %-8s P50 %.0f / P90 %.0f / max %.0f / 共 %d 轮"
              % (_label, statistics.median(_v), _p90, _v[-1], len(_v)))
    if not any(_num(r.get("t_screen")) for r in rows):
        print("\n[耗时] 样本没有 t_screen/t_quick(v2.3.33 及更早) —— 只能看到 L0, 算不出哪一段在变慢")

    # ---- 3. 阈值灵敏度: 唯一真正要回答的那个问题 ----
    usable = [r for r in rows if _num(r.get("st_cur")) and _num(r.get("st_cand"))]
    print("\n[门槛] 可判定的轮次 %d/%d(要求 st_cur 与 st_cand 都 > 0)" % (len(usable), len(rows)))
    if not usable:
        print("  没有可判定轮次 —— 这正是字段不够时的样子: 样本只有测量面, 没有判定输入。")
        return 0

    def decides(r, ratio):
        return r["st_cand"] > r["st_cur"] * ratio

    now_on = [r for r in usable if decides(r, r.get("wr") or WIN_RATIO.get(r.get("group"), 1.30))]
    flips = []
    same = 0
    for r in usable:
        cur = r.get("wr") or WIN_RATIO.get(r.get("group"), 1.30)
        if abs(cur - new_ratio) < 1e-9:
            same += 1                     # 假设值 == 当时门槛: 这一轮结构上不可能翻转
            continue
        a = decides(r, cur)
        b = decides(r, new_ratio)
        if a != b:
            flips.append((r, cur, a, b))
    new_on = [r for r in usable if decides(r, new_ratio)]
    print("  现行门槛会换 %d 轮; 改成 %g 后 %d 轮, 其中**翻转 %d 轮**"
          % (len(now_on), new_ratio, len(new_on), len(flips)))
    if same:
        # Cn1 的现行值恰好就是 1.15: 那种轮次"翻转 0"是"1.15 比 1.15", 不是"Cn1 不受影响"。
        print("  其中 %d/%d 轮的假设值与当时门槛相同(%g) —— 这些轮**结构上不可能翻转**, "
              "不要读成「该组不受影响」" % (same, len(usable), new_ratio))
    if flips:
        gain = [r["st_cand"] - r["st_cur"] for r, _c, _a, _b in flips if _b]
        print("  翻转方向: %s" % ("多换" if sum(1 for f in flips if f[3]) > len(flips) / 2 else "少换"))
        if gain:
            print("  多换的那几轮换来: 中位 %+.1f MiB/s, 合计 %+.1f MiB/s(轮次间不可相加, 合计只当量级)"
                  % (statistics.median(gain), sum(gain)))
        for r, cur, a, b in flips[:8]:
            print("    %s %-4s cur=%.1f cand=%.1f(现门槛 %s: %s / 新门槛: %s)"
                  % (r.get("ts", "?"), r.get("group", "?"), r["st_cur"], r["st_cand"], cur,
                     "换" if a else "保持", "换" if b else "保持"))
    else:
        print("  没有翻转: 在已有样本上这个门槛改动不影响任何一轮。注意样本量 n=%d, 只说明"
              "'过去这些轮不受影响', 不说明'以后也不受影响'。" % len(usable))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
