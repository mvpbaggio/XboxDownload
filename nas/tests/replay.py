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

v2.3.33: `screen_capped` 这一列的空**不再是一句话**。撞顶线是**每路**的(8MiB/0.1s=80MiB/s),
4 路并发下每路只有 20 —— 结构性撞不到, 所以这一列在现行档位下恒空。样本里的 `screen_cap`
就是那个理由: 有它 => "撞顶 0 行是这一档撞不到"; 没有它 => "旧版没记, 无法区分"。
不要拿 v2.3.32 及更早的样本读"Cn1 节点都没到 80"—— 那批样本证明不了这件事。
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
# v2.3.33 补 screen_cap: 撞顶线是**每路**的(80/wave), 没有它 "capped 空" 与 "capped 真为空" 同形。
WANTED = ("ts", "group", "l0", "l0_tail", "l0_pool", "l0_probed", "l0_alive",
          "screen", "screen_capped", "screen_skip", "screen_zero", "screen_wave", "screen_cap",
          "quick", "quick_skip", "t_l0",
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
    # v2.3.33: 撞顶线是**每路**的, 所以"0 行有撞顶"这句话在没有 screen_cap 时无法解释 ——
    # 4 路并发下每路只有 20MiB/s 的线, 结构上撞不到; 串行才是 80。两种"空"必须分开。
    caps = [r.get("screen_cap") for r in rows if r.get("screen_cap")]
    print("\n[screen] %d/%d 行有撞顶节点(值只是下界, 名次由 L0 握手时间决定)" % (capped_rows, len(rows)))
    if capped_rows == 0:
        if caps:
            print("[screen] 撞顶 0 行**有理由**: 本轮单路撞顶线 %s MiB/s(4 路是 80/4=20, 串行才是 80); "
                  "这一档撞不到 ≠ 节点没到 80" % sorted(set(caps)))
        else:
            print("[screen] 撞顶 0 行但样本没有 screen_cap —— 无法区分「这一档撞不到」与「真没人撞」"
                  "(v2.3.32 及更早的样本就是这个状态)")
    skipped = sum(len(r.get("quick_skip") or []) for r in rows)
    print("[quick] 累计预算跳过 %d 个候选(这些在 quick 里根本没有键, 与'测了是 0'不同)" % skipped)
    s_skip = sum(len(r.get("screen_skip") or []) for r in rows)
    s_zero = sum(len(r.get("screen_zero") or []) for r in rows)
    print("[screen] 累计没测到 %d 个 / 测了 0 分 %d 个(两者在 screen 里同样缺席, 含义相反)"
          % (s_skip, s_zero))
    waves = [v for r in rows for v in (r.get("screen_wave") or {}).values() if isinstance(v, int)]
    if waves:
        # 8MiB 的读数是"被几路平分之后"的数: 同一节点在 wave=1 和 wave=4 下本来就不该同分。
        print("[screen] 并发档位 %s(分数只在同档位之间可比; wave 越大, 每路分到的带宽越少)"
              % dict((w, waves.count(w)) for w in sorted(set(waves))))

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
