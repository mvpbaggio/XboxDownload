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

v2.3.37: **上面那句"分数 vs 并发"的前提也是错的**。`wave` 是"进场那一刻同批几路", 而提交顺序
= 池序(L0 RTT 升序)、worker 只有 4 个, 于是 wave<4 恒为本批最先提交的 3 个 —— 它编码 RTT 名次,
不是并发。实测 5 行样本每行 `{1:1, 2:1, 3:1, 4:45}`。2c 因此改名「分数 vs 进场顺序档位」并按
group 分表; "4 路分摊"改由跨 SCREEN_WORKERS 对照回答, 不再指望这张表。
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
          "screen_floor", "screen_need", "screen_workers",
          "quick", "quick_skip", "t_l0", "t_screen", "t_quick",
          # v2.3.38 (审查方 N4): `note` 是 `decision_note()` 的输出, 由 decisions 在
          # `persist_samples` 里 `_s.update(_dec.get(group))` 并进样本行 —— 整套系统里**唯一**
          # 承载"为什么这么判"的字段, 却是唯一不进完整率检查的字段(README 字段表 28 键, 这里原来
          # 只有 27, 缺的正是它)。
          # 后果很具体: `note` 整列消失(比如 decisions 里没带)时, [字段] 全绿。
          # 位置按 README 字段表的顺序插(q_cur 之后), 好让输出与那张表逐行对得上。
          "cur", "wr", "st_cur", "st_cand", "q_cur", "note",
          "ip", "speed", "decision")


# v2.3.39 (审查方 P1-B): 这八个键只有 `test_pool` 决出冠军时才会由 `decisions` 并进样本
# (`persist_samples` 里 `_s.update(_dec.get(group) or {})`)。坏夜(快测全 0)时 `dec` 里没有
# 这一组, 样本却**刻意留行**(v2.3.30 ③: 坏日子最该被记住) —— 于是这八列在那行上必然缺席,
# 而完整率检查把它算成"该写没写"。行内本来就有判别器(`decision` = `no winner ...`),
# 检查却没用 —— 这是"缺席原因必须可辨"的第六遍, 仍然漏在同一个函数里。
DECISION_KEYS = ("cur", "wr", "st_cur", "st_cand", "q_cur", "note", "ip", "speed")


def _absent_why(k, r):
    """这一行的这个键没有, 有没有**正当**理由? 有就返回理由, 没有返回 None。

    两条理由都从行内自证, 不靠外部知识:
      - decision 写着 `no winner ...` ⇒ 那一轮没冠军, 决策字段结构性缺席;
      - `screen` 为空而 `screen_floor` 在 ⇒ 新格式 + 这一轮粗筛 0 个, need 无从计算。"""
    _d = r.get("decision")
    if isinstance(_d, str) and _d.startswith("no winner") and k in DECISION_KEYS:
        return "坏夜无冠军"
    if k in ("screen_need", "screen_floor") and not r.get("screen") \
            and r.get("screen_floor") is not None:
        return "本行粗筛 0 个"
    return None


def _why_brief(k, exc):
    return "/".join(sorted(set(x for x in (_absent_why(k, r) for r in exc) if x)))


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
    # v2.3.38 (审查方 N2): 原来的口径是「每行必须有的键, 缺 N 行」—— 这个 N 在**两义合一**:
    # 老版本本来没有这个字段(样本是 append 的, 30 天的文件里必然横跨好几个 schema), 与新版本
    # 该写却没写, 打出来一模一样。而"缺席原因必须可辨"这条论证在这套系统里已经写过四遍
    # (quick_skip / l0_pool+probed+alive / screen_skip / screen_zero), 回放器自己的完整率检查
    # 是唯一没遵守的地方 —— 而这两周正要改 schema, 一改, 缺数常年非零, 这行就退化成"人眼忽略"。
    # 口径: 以**该字段首次出现的行号**为 schema 拐点, 只对拐点之后的行报缺; 并印出拐点本身。
    # 首现=第 0 行(全都有)时行为与原来完全一致。
    print("\n[字段] 每行必须有的键(按**首现行**分口径: 拐点之前的行按老格式跳过, 不算缺):")
    for k in WANTED:
        _first = next((i for i, r in enumerate(rows)
                       if k in r and r.get(k) is not None), None)
        if _first is None:
            # 一次也没出现过: 这**不是**"缺 N 行", 而是"要么本版没这个字段, 要么整列丢了" ——
            # 两者的区别要靠人看, 但至少要让人看见"一次都没有", 而不是一个可以忽略的百分比。
            print("  %-14s 全缺(%d 行一次都没有) —— 本版没这个字段, 或整列写丢" % (k, len(rows)))
            continue
        _lack = [r for r in rows[_first:] if k not in r or r.get(k) is None]
        # v2.3.39 (审查方 P1-B / P2-C): "缺席"还有第二种**正当**成因, 而它与"该写没写"同样同形。
        # (a) 坏夜无冠军: test_pool 返回 None 时 dec[name] 不赋值, 样本却刻意留行(v2.3.30 ③),
        #     于是 cur/wr/st_cur/st_cand/q_cur/note/ip/speed 八列必然没有 —— 30 天文件里只要
        #     有过一夜坏网络, 这八列就长期挂非零缺数, 检查当场退化成"人眼忽略"。
        # (b) 新格式的坏夜行: screen 为空 ⇒ 没有 max(wave) ⇒ screen_need 只能是 null。它既不是
        #     "老格式没这个字段"(2c 原来的标签), 也不是"该写没写"。
        # 判别器行内都有: decision 写着 `no winner ...`; screen_floor 在 ⇒ 新格式。
        _exc = [r for r in _lack if _absent_why(k, r)]
        _real = len(_lack) - len(_exc)
        if _real:
            _flag = "缺 %d 行(首现于第 %d 行; 其后的 %d 行里缺 %d, 之前的 %d 行按老格式跳过)" % (
                _real, _first, len(rows) - _first, _real, _first)
            if _exc:
                _flag += "; 另 %d 行有理由缺席(%s)" % (len(_exc), _why_brief(k, _exc))
        elif _exc:
            _flag = "ok(%d 行有理由缺席: %s —— 不是丢字段)" % (len(_exc), _why_brief(k, _exc))
        elif _first:
            _flag = "ok(%d 行都有; 首现于第 %d 行, 之前的 %d 行老格式不算缺)" % (
                len(rows) - _first, _first, _first)
        else:
            _flag = "ok"
        print("  %-14s %s" % (k, _flag))

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
            # v2.3.38 (审查方 N1 第 3 点): 上面那行把 need 打成**一个集合**, 两种配置并排时
            # `[80.0, 320.0]` 读不出哪个是 W=1 哪个是 W=4 —— 而这两个数的区别就是整段 2c 的分桶键。
            # 有不止一种 need 时逐行点名(前 8 行), 让"哪一轮是哪个配置"当场可查。
            if len(set(needs)) > 1:
                # v2.3.39: 原来这里写"换成几路=need/80"—— 那个换算把派生量当配置, 正是 P1-A 的病根。
                # 现在有 screen_workers 就打它, 没有就说"未记", **不反推**。
                print("[screen] 本文件里出现了 %d 种 need —— 逐行点名(前 8 行; need 是派生量,"
                      " 配置看 W):" % len(set(needs)))
                _shown_n = 0
                for _r in rows:
                    if not _r.get("screen_need"):
                        continue
                    _wo = _r.get("screen_workers")
                    _wtxt = ("W=%d" % _wo) if isinstance(_wo, (int, float)) and _wo > 0 else "W 未记"
                    print("[screen]   %s %s: %s, need %g MiB/s(max 档 %d)"
                          % (_r.get("ts"), _r.get("group"), _wtxt, _r["screen_need"],
                             int(round(_r["screen_need"] / 80.0))))
                    _shown_n += 1
                    if _shown_n >= 8:
                        break
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
        # v2.3.37 (审查方 P1-1): 连"并发档位"这个名字都不对 —— 见 2c 的说明。
        print("[screen] 进场顺序档位 %s(进场时刻同批请求数; 它编码**提交顺序**而不是并发, 见 2c)"
              % dict((w, waves.count(w)) for w in sorted(set(waves))))

    # ---- 2c. 分数 vs **进场顺序**档位(观测, 不判定) ----
    # v2.3.36 (审查方 M1): v2.3.35 我在三处写了"逐 IP 的 wave 没入库", 那是**事实错误** ——
    # `screen_wave` 一直是 {ip: 进场时同批几路} 逐 IP 落盘的(build_sample), 上面 143 行本来就
    # 把它和 `screen` 配对取出来了(只是只拿了 max 去算出口下界, 没算逐 IP)。
    #
    # v2.3.37 (审查方 P1-1): 但 v2.3.36 接着写的那句"单行内 wave 常无方差(满编 48 个全是 4),
    # 要跨档位攒"**也是错的**, 而且这个错更贵 —— 它把一段错误观测包装成"以后会有答案":
    #   `wave` = `_inflight[0]`(进场那一刻 +1 后读), 而提交顺序 = screen_rows = **L0 RTT 升序**,
    #   4 个 worker 只在**起步**时才产生 1/2/3。所以 wave<4 恒为本批最先提交的 3 个节点、
    #   wave=4 是其余全部: 真机 5 行样本每行都是 `{1:1, 2:1, 3:1, 4:45}`(45/48, 不是 48)。
    #   它编码的是 **RTT 名次**, 不含"被平分了几份"的信息 —— 48 个自始至终都跑在 4 路并发下。
    # 后果很具体: 若分数只与 RTT 名次有关而与并发无关(零因果), 这张表照样打出
    # "wave=1 中位 78 / wave=4 中位 60", 读起来是"4 路把 78 打到 60, 分摊严重" —— 实际 100% 是
    # 选择偏倚。而且这套池序由 L0 min-of-3 决定, 名次会在行间翻动(实测 8 行就出了 8 个同 IP
    # 跨档位), 于是"同 IP 对照"也不干净: 那个 IP 的 RTT 名次本来就变了。
    # 所以这一段只保留它**真能回答**的那个问题: 「RTT 名次前 3 的节点, 分数是否更高」。
    # "4 路分摊"只能靠跨 SCREEN_WORKERS 配置对照(同一批节点, 1/2/4 路各跑几轮, 比同 IP 中位),
    # 那是**排出来的实验**, 不是攒出来的 —— 台账里这颗挂账的判定口径已改。
    # 另外按 group 分表: WIN_RATIO / DEGRADE_MIN_RECORD 在别处就是按两组两个分布处理的, 一次
    # `--pool Cn2` 的定向重测会把整行的组构成挪一次, 混在一张表里中位数就会跟着漂(审查方 P2-3)。
    # v2.3.39 (审查方 P1-A) —— **上一版的分桶键是错的, 这一条把它换掉了**:
    # v2.3.38 拿 `screen_need` 当"配置指纹"(need = 80 × max(进场档位)), 理由是"W=1 记 80、
    # W=4 记 320"。错在 `max(进场档位)` 是**观测值**, 它的上限由 `len(screen_rows)` 决定而不只由
    # SCREEN_WORKERS 决定:
    #   - 池里只活 3 个节点 → wave {1,2,3} → need=240, 与"W=3 的配置"**逐字同形**;
    #   - budget 截断压掉的是**被测节点数**, 不是 max 档: 48 个节点只测 5 个, wave 仍是 {1,2,3,4}
    #     ⇒ need 仍是 320(4 个 worker 一提交就各领一个)。v2.3.38 那句"预算提前收手时 max 档 <
    #     SCREEN_WORKERS(4 路只跑到 3 档)"是**假的**, 这一版把它删掉。
    # 后果实测(合成 3 行, **全部 W=4**, 只有第 1 行是"劣化夜 L0 只剩 3 个"): 旧口径打出
    # `Δ +52.0 (+186%)` 并标成「这才是『4 路分摊』的答案」—— 那个 Δ 从头到尾没有并发参与, 它是
    # "劣化夜 vs 健康夜"。也就是说**只要有一夜 L0 只活下来 1-3 个节点, 2c 就会在从未做过实验的
    # 前提下凭空产出一份带百分号的"证据"**。触发条件窄, 但产出的东西看起来极硬。
    # 修: 分桶键改成 **screen_workers**(样本 v2.3.39 起直接记 SCREEN_WORKERS —— 真的配置);
    # `need` 降级为派生量, 每行验 `need == 80 × max(wave)`, 不成立就点名那一行(那说明生产代码
    # 改了推导口径), 拿它分桶的事不再做。跨 W 的同 IP 中位差才是『4 路分摊』。
    _CELL_CEIL = 80.0     # 与主程序 SPEED_CEIL_MIB 同值; 回放器不 import 主程序(要能单文件跑)
    _cells = {}   # {(group, W): {"rows","nowave","bad","pair","unpair","needs","wave"}}
    _ipcfg = {}   # {group: {ip: {W: {wave: [scores]}}}}
    _needbad = []  # [(ts, group, need, 期望值)] —— need 与 wave 推导不一致的行
    for _r in rows:
        _g = _r.get("group") or "?"
        _wo = _r.get("screen_workers")
        _w = int(_wo) if isinstance(_wo, (int, float)) and _wo > 0 else None
        _c = _cells.setdefault((_g, _w),
                               {"rows": 0, "nowave": 0, "bad": 0, "pair": 0, "unpair": 0,
                                "needs": {}, "wave": {}})
        _c["rows"] += 1
        _wv = _r.get("screen_wave") or {}
        if not _r.get("screen"):
            # 粗筛一个都没测到(坏夜): 这一行**没有** wave 是结构性的, 不是"老格式没记"
            _c["bad"] += 1
        elif not _wv:
            # 审查方观察项: "N 行"数的是**样本行**, 不是"有 wave 的行" —— 一个桶里 4 行、其中 3 行
            # 整行没有 wave(老格式), 读者会以为那 4 行都进了中位数。单独记一个数, 让差额可见。
            # 与 `bad` 互斥: 坏夜行已经单独点名, 算进这里会重复计一次。
            _c["nowave"] += 1
        _nd = _r.get("screen_need")
        if isinstance(_nd, (int, float)) and _nd > 0:
            _c["needs"][_nd] = _c["needs"].get(_nd, 0) + 1
            if _wv:
                _exp = _CELL_CEIL * max(_wv.values())
                if abs(_exp - _nd) > 1e-6:
                    # need 是派生量, 推导口径一变(比如改成按池子大小算)这里的等式就断。
                    # 不参与分桶, 只点名 —— 沉默地"按 need 分表"正是上一版出错的方式。
                    _needbad.append((_r.get("ts"), _g, _nd, _exp))
        for _sp, _ip in (_r.get("screen") or []):
            _wv1 = _wv.get(_ip)
            if not isinstance(_wv1, int) or _wv1 <= 0:
                # v2.3.38 (审查方 N2 后半): 原来这里**静默 continue** —— 真机 383 条 screen 条目
                # 里有 143 条(整整 3 行)就是这么消失的, 全是 v2.3.32/33 的老行(那时还没 screen_wave)。
                # 老行合理, 但"合理"和"没人知道"是两件事: 不计数的话, 读者会以为 n= 覆盖了所有行。
                _c["unpair"] += 1
                continue
            _c["pair"] += 1
            _c["wave"].setdefault(_wv1, []).append(float(_sp))
            _byip = _ipcfg.setdefault(_g, {})
            _byip.setdefault(_ip, {}).setdefault(_w, {}).setdefault(_wv1, []).append(float(_sp))

    def _med(v):
        v = sorted(v)
        return v[len(v) // 2] if len(v) % 2 else (v[len(v) // 2 - 1] + v[len(v) // 2]) / 2.0

    def _wlab(_w):
        # 只有"没记"这一种未知。**不再有"need=240 所以是 3 路"** —— 那是把观测值当配置
        # (P1-A)。老行的 W 未知就说未知, 不按 need 反推。
        return ("W=%d" % _w) if _w else "W=?(未记, v2.3.38 及更早)"

    if _cells:
        print("\n[obs] 分数 vs 进场顺序档位 —— **按 (组, 配置 W= 实际并发数) 分表**")
        print("      配置指纹 = **screen_workers**(这一行实际配置的并发数), **不是 screen_need**:")
        print("      need = %g × max(进场档位) 是**派生量**, 而 max 档是观测值 ——" % _CELL_CEIL)
        print("      池里只活 3 个节点时, 4 路配置也只到档位 3 ⇒ need=240, 与「W=3 的配置」逐字同形;")
        print("      反过来 budget 截断压掉的是**被测节点数**、不是 max 档(48 个只测 5 个, wave 仍")
        print("      是 {1,2,3,4}) ⇒ **跨 need 的差不等于并发对照**(那可能是劣化夜 vs 健康夜)。")
        for _key in sorted(_cells, key=lambda k: (str(k[0]), k[1] if k[1] else -1)):
            _c = _cells[_key]
            _g, _w = _key
            _tail = ""
            if _c["bad"]:
                _tail += ", 其中 %d 行粗筛 0 个(坏夜无结果)" % _c["bad"]
            if _c["nowave"]:
                # 有分数却整行没 wave(老格式): 它们**不计入**下面的 n, 差额得让读者看见
                _tail += ", 另有 %d 行有分数但没有 wave(老格式, 不进表)" % _c["nowave"]
            if not _c["pair"]:
                print("   [%s · %s] %d 行%s · 可配对 0 条 —— 整桶不进表(wave 全缺就分不出档位)"
                      % (_g, _wlab(_w), _c["rows"], _tail))
                continue
            print("   [%s · %s] %d 行%s · 可配对 %d 条 / 配不上 %d 条"
                  % (_g, _wlab(_w), _c["rows"], _tail, _c["pair"], _c["unpair"]))
            if len(_c["needs"]) > 1:
                # 同一个 W 下出现不止一种 need —— 那**不是**两种配置, 是"那一夜进场几个节点"不同。
                # 打出来, 因为这个数最容易被下一个人当成分桶键(上一版就是我)。
                print("       派生 need: " + ", ".join(
                    "%g(max 档 %d) ×%d 行" % (n, int(round(n / _CELL_CEIL)), k)
                    for n, k in sorted(_c["needs"].items()))
                      + " —— 同一 W 下 need 不同**不是配置不同**, 是那一夜进场的节点数不同")
            for _wv2 in sorted(_c["wave"]):
                _v = sorted(_c["wave"][_wv2])
                print("       wave=%d  n=%-4d 中位 %5.1f  极差 %.1f~%.1f"
                      % (_wv2, len(_v), _med(_v), _v[0], _v[-1]))
            if max(_c["wave"]) < 2:
                print("       (本配置内只有一个档位 —— 没有档位可比, 正常)")

        if _needbad:
            # v2.3.39 (审查方 P1-A 的后半): need 是派生量, 它的定义蕴含 `need == 80 × max(wave)`。
            # 这条等式断了只有一种解释: 生产代码换了推导口径(比如按池子大小算), 而**那一版的所有
            # 分桶结论都不再有意义**。不参与分桶, 点名。
            print("\n       **%d 行的 screen_need 与 80 × max(screen_wave) 不符** —— 推导口径变了,"
                  " 不是配置不同(前 4 行):" % len(_needbad))
            for _ts0, _g0, _nd0, _exp0 in _needbad[:4]:
                print("         %s %s: need %g, 但 80×max(wave)=%g" % (_ts0, _g0, _nd0, _exp0))

        _cross = 0
        for _g in sorted(_ipcfg):
            _cand = []
            for _ip, _byw in _ipcfg[_g].items():
                _ws = sorted([w for w in _byw if w])
                if len(_ws) < 2:
                    continue          # 老行(W 未记)不参与: 不知道跑在几路下, 不猜
                _series = []
                for _w in _ws:
                    _sc = [s for _v in _byw[_w].values() for s in _v]
                    _series.append((_w, _med(_sc), len(_sc)))
                _d = _series[-1][1] - _series[0][1]
                _ok_n = all(n >= 2 for _, _, n in _series)
                # v2.3.39 (审查方 P2-D): 取前 8 原来按 **IP 字典序** —— 48 个 IP 时等于按网段取,
                # 可能整段偏一个机房(同配置段上一轮已为同一个理由改掉)。这里改成按 |Δ| 从大到小:
                # 要看的就是"差得最大的那几个"; n 不够的排最后。
                _cand.append((abs(_d) if _ok_n else -1.0, _ip, _series, _d, _ok_n))
            if not _cand:
                continue
            _cand.sort(key=lambda t: (-t[0], t[1]))
            print("\n       同 IP 跨配置 [%s](**W 不同 = 配置不同** —— 这一段才是『4 路分摊』的"
                  "答案; 同一 W 内档位不同仍只说明 RTT 名次):" % _g)
            for _mag, _ip, _series, _d, _ok_n in _cand[:8]:
                _cross += 1
                _parts = ["W=%d 中位 %5.1f(n=%d)" % (w, m, n) for w, m, n in _series]
                # v2.3.39 (审查方 P3-E): Δ 是"不同时刻两个中位数相减", 一侧只有 1 个样本时
                # 它等于拿一次测量代表一档。两侧 n 都 >=2 才报 Δ, 否则明说"n 不足" ——
                # 报一个光秃秃的百分号比不报更坏(C96 上一版还把它钉成了期望值, 已改)。
                if _ok_n and _series[0][1]:
                    _tail2 = "   Δ %+.1f (%+.0f%%)" % (_d, _d / _series[0][1] * 100.0)
                else:
                    _tail2 = "   Δ 不报(一侧 n<2: %s)" % " / ".join(
                        "n=%d" % n for _, _, n in _series)
                print("         %-15s " % _ip + "  |  ".join(_parts) + _tail2)
        if not _cross:
            _seen = sorted(set(n for (_gg, n) in _cells if n))
            _desc = "/".join("W=%d" % n for n in _seen) if _seen else "? (配置未记录)"
            # 有 W 缺失的行时必须说出来: 它们**不参与**跨配置对照(不知道跑在几路上), 只印
            # "只有一种配置"会让读者以为剩下的行也都确认在那一档。
            _unk = sum(_c["rows"] for _k2, _c in _cells.items() if not _k2[1])
            _extra = ("; 另有 %d 行未记 screen_workers(配置未知, 不参与跨配置对照)" % _unk) if _unk else ""
            print("\n       跨配置对照为空: 能确认的配置只有 %s 一种%s —— 『4 路分摊』只能"
                  "**排** SCREEN_WORKERS 取 1、2、4 三档各跑几轮再比, 攒不出来" % (_desc, _extra))

        # 同配置内的档位变动: 保留, 但把话说明白 —— 同一个 need 内它**不是**并发效应。
        _any = False
        for _key in sorted(_cells, key=lambda k: (str(k[0]),
                                                 k[1] if isinstance(k[1], (int, float)) else -1)):
            _c = _cells[_key]
            if len(_c["wave"]) < 2:
                continue
            # 先收齐"真的跨了档位"的 IP 再印表头 —— 否则会出现只有标题、下面一行的空表,
            # 那种表比不印更容易被误读成"没有变化"。
            _multi = []
            for _ip, _byn in (_ipcfg.get(_key[0]) or {}).items():
                _d = _byn.get(_key[1]) or {}
                if len(_d) >= 2:
                    _multi.append((_ip, _d))
            if not _multi:
                continue
            _any = True
            print("\n       同 IP 同配置内的档位变动 [%s · %s](= 该 IP 在不同行的 **进场档位/RTT 名次**"
                  " 变了; 同一 W 内**不含**「换了几路跑」的证据 —— 若这些行的 need 也不同, 那是那一夜"
                  "存活节点数不同, 与配置无关; 前 6 个):" % (_key[0], _wlab(_key[1])))
            # 审查方观察项: 原来按 IP 字符串取前 6, 48 个 IP 时等于按 IP 段取 —— 可能整段偏一个
            # 机房。改成按**档位间跨度**从大到小: 要看的本来就是"变化最大的那几个"。
            def _span(_t):
                _m = [_med(_v) for _v in _t[1].values()]
                return -(max(_m) - min(_m)) if _m else 0.0
            _multi.sort(key=_span)
            for _ip, _d in _multi[:6]:
                print("         %-15s " % _ip + "  ".join(
                    "wave=%d 中位%5.1f(n=%d)" % (_w, _med(_v), len(_v))
                    for _w, _v in sorted(_d.items())))
        if not _any:
            print("\n       没有任何 IP 在**同一配置内**跨两个档位 —— 档位间中位数仍可看, "
                  "但归因不到单个节点(同一配置内的档位差本来也只说明 RTT 名次)")
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
