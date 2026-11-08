#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交付前机器审计 (2026-11-06, python3.8 兼容)

为什么有这个东西: 前几轮"审计"是我人眼读代码, 每次只发现"这次盯的那块",
于是每被叫一次就冒一个新问题。人读不可复现、不可累积、不能拦回归。
这里把三类断言固化成可执行文件, 每次交付前跑一次, 全 PASS 才允许发:

  A 静态契约  跨文件结构一致性(组名/域名表/测速对/环境变量/README 常量/版本号)
              含 A18: 项目上一级的 AI共管台账.md 必须严格倒序(头部说'新条目加在最上面',
              文件就得真的是倒序 —— 只读头部的人会拿一份过期 48 小时的状态当现状)
              —— v2.3.3 的"止血只切半个组"就是这一类, 靠人眼漏了两次
  B 失败出口棘轮  AST 统计每个函数的失败出口数(return False/None/0.0 + except),
              与登记表比对。任何新增失败出口都会让审计变红, 强制补用例。
              —— 防"每次修完又冒新问题"的核心机制: 未覆盖的失败路径变成会自鸣的数字
  C 行为契约  决策分支/整组切换/幂等/下限/预算/合并落盘, 全桩 + 确定性, 不碰网络
  D 交付卫生  凭据/私网指纹/换行符/README 完整性

用法:
  python3 tests/audit.py             # 审脚本所在目录的上一级
  python3 tests/audit.py --dir DIR   # 审指定目录(变异自检用)
  python3 tests/audit.py --print-exits   # 打印当前失败出口统计(改完代码后据此更新登记表)
退出码: 0 全过 / 1 有 FAIL
"""
import argparse
import ast
import contextlib
import hashlib
import importlib.util
import inspect
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time

FAIL_CONSTS = (False, None, 0, 0.0, "")

# ---------------------------------------------------------------- 失败出口登记表
# (文件, 函数) -> 失败出口数。数字变化 = 有人新增/删除了一条失败路径, 必须重新审视
# 并在 FAILURE_TESTS 里给出对应用例名, 否则不批交付。
FAILURE_EXITS = {
    ("xbox-speedtest.py", "dl_speed"): 3,          # 非 200/206、异常、正常返回路径里的 0 值
    ("xbox-speedtest.py", "dl_steady"): 3,          # v2.3.19: IfExp 补数后 +1(0.0 if not got else ...)
    ("xbox-speedtest.py", "mark_retest"): 2,        # v2.3.14: 标记写不进去不得影响测速
                                                    # v2.3.34: +1 = 读旧标记失败(旧格式/JSON 坏)时按空表继续 ——
                                                    #   读不出旧格绝不能阻碍写本轮格, 否则冷却永久失效
    ("xbox-speedtest.py", "median_of"): 1,          # v2.3.19: `median(good) if good else 0.0`
    ("watchdog.py", "retest_cooldown_left"): 8,     # v2.3.14: 无标记/读不出、未来时间戳、冷却已过
                                                    # v2.3.34 3 -> 8 (P2-D 按组读): 空文件、dict 里没这组、
                                                    #   json 解析失败后回退裸数字、裸数字也解析失败 —— 四条新出口
                                                    #   方向一致(读不出就当没冷却, 宁可多测一次)。它**不是**
                                                    #   "越写越安全": 任何一条改成 return 大数都会把重测冻死,
                                                    #   C48c(无标记)/C48d(未来时间戳)会在那两种上抓
    ("xbox-speedtest.py", "op_of"): 2,             # v2.3.5 新增: 空标签 / 域名型标签 -> 未知运营商
    ("xbox-speedtest.py", "test_pool"): 1,          # 全部候选无有效结果 -> None
    ("xbox-speedtest.py", "decision_note"): 3,      # v2.3.10: 轮次不足/阈值无效、判定本来就稳 -> 不刷日志
                                                    # v2.3.35 2 -> 3 (N3): 拆出"一轮都没出数"这一条 ——
                                                    #   它跟"本来就稳"不是一件事(前者是没数据, 后者是数据太齐),
                                                    #   合在一个 if 里正是极差吃 0 值的同一个毛病的邻居
    ("xbox-speedtest.py", "router_cmd"): 3,         # v2.3.19: `... if rc==0 else None` 这条也被数进来了
    ("xbox-speedtest.py", "router_apply"): 3,       # v2.3.5: no change / uci 失败 / restart 失败
    ("xbox-speedtest.py", "main"): 3,               # 锁占用、路由器不可达、两组无结果分支
                                                    # v2.3.34 曾 +2 = results.json 读段的两个 except 处理器
                                                    # v2.3.35 3 -> (回落到) 3: 那一段整块搬进 persist_results(),
                                                    #   计数跟着函数走, 不是"少了两条失败路径" —— 别把回落
                                                    #   当成变安全: 真正的失败处置现在在下面的 persist_results
    ("xbox-speedtest.py", "persist_results"): 5,    # v2.3.35 (N1): 读不了的 FileNotFoundError(正常态)、
                                                    #   读坏的 except、隔离失败、写盘失败。方向都是"不让故障
                                                    #   自我维持": 前两个 -> 隔离并重建, 后两个 -> 说清留证据/重试
                                                    # v2.3.36 4 -> 5 (M2): +1 = `.seen` 标记写不成(只让下一轮
                                                    #   的 created 退化成有损, 不让整轮失败)。**这条是唯一一条
                                                    #   "故意吞掉"的**, 所以必须在这里写清方向 —— 别把数字变大
                                                    #   当成"越来越安全"
    ("watchdog.py", "router_cmd"): 3,               # v2.3.19: 同上, 三元 else None
    ("watchdog.py", "router_apply"): 2,             # uci 失败、restart 失败
    ("watchdog.py", "steady"): 3,
    ("watchdog.py", "canary"): 1,                   # v2.3.28: 连不上/非 2xx 各一个出口
                                                    # v2.3.34 2 -> 1 (P3-F): 删掉 return False, 判据只剩 state;
                                                    #   剩的 1 是 except 分支那个裸 return(写完 failed 再返回)
    ("xbox-speedtest.py", "write_samples"): 1,      # v2.3.28: 落盘失败 -> 打 warn 并返回已写行数
    ("watchdog.py", "load_hotspares"): 1,           # results.json 读不出
    ("watchdog.py", "load_record"): 7,              # v2.3.5: 多一层 pinned_ip 匹配容错(原 6)
    ("watchdog.py", "quick_fix"): 1,                # 无达标热备 -> None
    ("watchdog.py", "run_lock_taken"): 5,           # v2.3.16: 3 except + None/False(建不了改走 LOCK_ERR)
    ("watchdog.py", "record_pin"): 4,               # v2.3.16: 不是 list / 没有该组 / except / 写失败
    ("watchdog.py", "get_pins"): 1,                 # v2.3.19: ssh 不可达返回 None(与"可达但空配置"的 {} 分开)
    ("watchdog.py", "main"): 3,                     # 锁占用、路由器不可达、v2.3.18 重测子进程未正常结束
    ("pool_refresh.py", "refresh"): 1,             # 单域名解析失败不中断
    ("pool_refresh.py", "ip_reject_reason"): 2,    # v2.3.25: `except ValueError` + `return None`(=放行)。
                                                   # 两条都不是"失败", 但棘轮不分语义: 下一个人想加一条
                                                   # 静默放行的分支(裸 except: return None), 数量会变、B1 立刻红。
                                                   # 两个真正拒收的分支返回的是字符串, 不被数进来。
    ("xbox-speedtest.py", "_screen"): 1,           # v2.3.25: 线程池里 future.result() 抛异常 -> 记 0.0
    ("xbox-speedtest.py", "_rtt_min"): 1,          # v2.3.26: `except Exception: continue`(握手失败不是 0ms,
                                                   #   是"不可达", 直接不进候选 —— 记 0.0 会让死节点排第一)
    ("xbox-speedtest.py", "rtt_rank"): 1,          # v2.3.26: 线程里抛的一律按不可达, 与 _rtt_min 同口径
}

# 每个含失败出口的函数, 至少要有这些用例覆盖(用例名与下方 C 段一致)
FAILURE_TESTS = {
    ("xbox-speedtest.py", "dl_speed"): ["C1"],
    ("xbox-speedtest.py", "dl_steady"): ["C1", "C28"],
    ("xbox-speedtest.py", "op_of"): ["C25"],
    ("xbox-speedtest.py", "test_pool"): ["C17"],
    ("xbox-speedtest.py", "decision_note"): ["C41", "C41b"],
    ("xbox-speedtest.py", "router_cmd"): ["C21"],
    ("xbox-speedtest.py", "router_apply"): ["C29"],
    ("xbox-speedtest.py", "main"): ["C19", "C20", "C21", "C22", "C27"],
    ("xbox-speedtest.py", "mark_retest"): ["C49c"],
    ("xbox-speedtest.py", "persist_results"): ["C86b"],
    ("xbox-speedtest.py", "median_of"): ["C63"],
    ("watchdog.py", "retest_cooldown_left"): ["C48c", "C48d"],
    ("watchdog.py", "router_cmd"): ["C9", "C10"],
    ("watchdog.py", "router_apply"): ["C7", "C8", "C9", "C10"],
    ("watchdog.py", "steady"): ["C1"],
    ("watchdog.py", "canary"): ["C71"],
    ("xbox-speedtest.py", "write_samples"): ["C72"],
    ("watchdog.py", "load_hotspares"): ["C13"],
    ("watchdog.py", "load_record"): ["C13", "C26", "C65"],
    ("watchdog.py", "quick_fix"): ["C3", "C5", "C6"],
    ("watchdog.py", "run_lock_taken"): ["C53", "C55"],
    ("watchdog.py", "record_pin"): ["C56", "C56b"],
    ("watchdog.py", "get_pins"): ["C64", "C64b"],
    ("watchdog.py", "main"): ["C2", "C21b", "C26", "C62"],
    ("pool_refresh.py", "refresh"): ["C24"],
    ("pool_refresh.py", "ip_reject_reason"): ["C66"],
    ("xbox-speedtest.py", "_screen"): ["C67"],
    ("xbox-speedtest.py", "_rtt_min"): ["C68"],
    ("xbox-speedtest.py", "rtt_rank"): ["C69", "C70"],
}

RESULTS = []


def chk(cid, title, cond, detail=""):
    cond = bool(cond)
    RESULTS.append((cid, title, cond))
    line = ("PASS " if cond else "FAIL ") + cid + "  " + title
    if not cond and detail:
        line += "\n      why: " + str(detail)[:400]
    print(line)
    return cond


class _OffResp(object):
    """canary 离线桩的响应: 200/206 都算活, 让 canary 静默、决策链不受影响。"""
    status = 206
    def close(self):
        pass


class _OffConn(object):
    """canary 离线桩的连接对象。同时顶上 `http.client.HTTPConnection` 与 `BindIPConn` 两个位置:
    前者是正常路径, 后者是"绑 IP"那条路 —— 变异 M86 一注入就从后者出门。"""
    def __init__(self, host=None, *a, **k):
        pass
    def request(self, method, path, headers=None):
        pass
    def getresponse(self):
        return _OffResp()
    def close(self):
        pass


def _offline_http():
    """给 watchdog 模块用的 `http` 替身(只需要 .client.HTTPConnection / .HTTPSConnection)"""
    return type("_OffHTTP", (object,),
                {"client": type("_OffClient", (object,),
                                {"HTTPConnection": _OffConn, "HTTPSConnection": _OffConn})})


def load(path, tag):
    """每次给唯一模块名, 避免同一轮里多次加载互相污染(桩打歪曾让冒烟骗过我一次)

    v2.3.26: 主程序现在**先跑 L0 RTT 预筛**才粗筛, 而 `rtt_rank` 是真的 TCP 握手。
    不桩它, 每一条走 test_pool/main 的用例都要对外面 1.1.x.x / 2.2.x.x 打
    RTT_TRIES×RTT_TIMEOUT = 4.5s 超时(实测单次 audit 从 1.4s 涨到 94s, user 时间 0 —— 全在等)。
    审计必须能离线跑, 所以这里默认换成"全池都通、ms 递增"的桩; 真实现留在 `_rtt_rank_real`,
    由 C68/C69/C70 显式取用(那三条要验的正是排序/预算/标签无关)。
    """
    global _SEQ
    _SEQ += 1
    name = "audit_%s_%d" % (tag, _SEQ)
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    if hasattr(m, "rtt_rank") and callable(getattr(m, "rtt_rank", None)):
        m._rtt_rank_real = m.rtt_rank

        def _rtt_stub(rows, port=443, top=10 ** 9, budget=0.0, tail=None, stats=None):
            out = [(float(i), ip, tag) for i, (ip, tag) in enumerate(rows)]
            # 桩也要填 tail/stats: 生产调用点传了这两个出口, 桩不填的话 l0_tail 恒空、
            # l0_pool 恒 None —— 那让 A24 的两条新断言变成"恒真", 等于没测。
            if tail is not None:
                tail.extend(round(r[0], 1) for r in out[top:top + 8])
            if stats is not None:
                stats.update({"pool": len(rows), "probed": len(rows), "alive": len(rows)})
            return out[:top]
        m.rtt_rank = _rtt_stub
    if hasattr(m, "canary") and hasattr(m, "BindIPConn"):
        # v2.3.31: `watchdog.main` 现在每小时真的走一次 canary_gate()。审计里有十几条用例
        # 用各自的桩跑 main(冷却/止血/重测/跨组), 每条都会因此去 HEAD 一个真 URL ——
        # 有网时几十毫秒看不出来, 没网或变异把 canary 改成绑 IP 时每条卡满 DL_TIMEOUT=12s,
        # NAS 侧实测把 selftest 的 300s subprocess 超时跑穿(M86)。
        # 这里集中桩住**两条出门的路**(http.client 与 BindIPConn), 需要控制状态码的用例
        # (C71/C73)在 load 之后自行覆盖。审计必须离线可跑, 不能靠"本机恰好能连上"。
        m.http, m.BindIPConn = _offline_http(), _OffConn
    return m


_SEQ = 0


def capture(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = fn(*a, **kw)
    return r, buf.getvalue()


# 运行产物(只在真机上生成, 不进仓库): 审计不许碰其中任何一个字节。
# 2026-11-07 实测: v2.3.28 把 samples.jsonl 路径写成模块常量, 一次 audit 通过 st_run 跑 38 次 main,
# 每次都往**真**文件 append 一行夹具样本(9.9.9.9 / 1.0ms) —— 39 行里只有 1 行是真机测出来的。
# 靠人眼盯「哪些用例改了 BASE」不成立(同类路径还有 results/speed.log/.last_retest/run.log),
# 所以这里改成事后对账: 跑前跑后各拍一次 (size, mtime_ns, md5), 不讲道理、只讲事实。
RUN_PRODUCTS = ("samples.jsonl", "results.json", "speed.log", ".last_retest", "run.log")


def run_products_state(d):
    st = {}
    for n in RUN_PRODUCTS:
        p = os.path.join(d, n)
        try:
            with open(p, "rb") as f:
                blob = f.read()
            s = os.stat(p)
            st[n] = (len(blob), s.st_mtime_ns, hashlib.md5(blob).hexdigest())
        except OSError:
            st[n] = None       # 不存在也算一种状态: 审计把它造出来同样是污染
    return st


def readme_name(d):
    """镜像目录里叫 nas-README.md, 仓库里叫 README.md —— 同一套审计要能审两种布局"""
    for n in ("nas-README.md", "README.md"):
        if os.path.exists(os.path.join(d, n)):
            return n
    return "nas-README.md"


# ------------------------------------------------------------------ A 静态契约
def audit_static(d):
    print("\n--- A 静态契约 ---")
    xst = load(os.path.join(d, "xbox-speedtest.py"), "xst")
    wd = load(os.path.join(d, "watchdog.py"), "wd")
    pr = load(os.path.join(d, "pool_refresh.py"), "pr")
    rd = open(os.path.join(d, readme_name(d)), encoding="utf-8").read()

    groups = [p[0] for p in xst.POOLS]
    chk("A1", "组名四处一致 (speedtest/POOLS, watchdog/DOMAIN/TESTPATH/DOMAINS)",
        groups == list(wd.DOMAIN) == list(wd.TESTPATH) == list(wd.DOMAINS),
        "groups=%s DOMAIN=%s TESTPATH=%s DOMAINS=%s"
        % (groups, list(wd.DOMAIN), list(wd.TESTPATH), list(wd.DOMAINS)))

    bad = []
    for name, path, host, urlp, doms in xst.POOLS:
        if set(wd.DOMAINS.get(name, ())) != set(doms):
            bad.append("%s DOMAINS=%s vs POOLS[4]=%s" % (name, wd.DOMAINS.get(name), doms))
        if wd.DOMAIN.get(name) != doms[0]:
            bad.append("%s DOMAIN=%s != doms[0]=%s" % (name, wd.DOMAIN.get(name), doms[0]))
        if wd.TESTPATH.get(name) != urlp:
            bad.append("%s TESTPATH 与 speedtest 测速路径不一致" % name)
        base = os.path.basename(path)
        if base in pr.POOLS and set(pr.POOLS[base]) != set(doms):
            bad.append("%s pool_refresh 域名集=%s 与 POOLS[4] 不一致" % (name, pr.POOLS[base]))
    chk("A2", "每组域名集合三处一致 (POOLS[4] / DOMAINS / pool_refresh)", not bad, "; ".join(bad))

    chk("A3", "WIN_RATIO 覆盖所有组且值在合理区间(1.05~1.5)",
        set(xst.WIN_RATIO) == set(groups) and all(1.05 <= v <= 1.5 for v in xst.WIN_RATIO.values()),
        "WIN_RATIO=%s groups=%s" % (xst.WIN_RATIO, groups))

    # README 里的数字必须等于代码里的实际常量, 否则文档会变成假承诺
    pairs = [
        ("MAX_QUICK_CANDS=16", xst.MAX_QUICK_CANDS, 16),
        ("QUICK_BUDGET_SEC=420", xst.QUICK_BUDGET_SEC, 420.0),
        ("DEGRADE_RATIO 55%", wd.DEGRADE_RATIO, 0.55),
        ("THRESHOLD_MIBS 8", wd.THRESHOLD_MIBS, 8.0),
        ("HOTSPARE_MIN_MIBS 20", wd.HOTSPARE_MIN_MIBS, 20.0),
        ("HOTSPARE_N 3", xst.HOTSPARE_N, 3),
        ("STEADY_ROUNDS 3", xst.STEADY_ROUNDS, 3),
        ("QUICK_MAX_SEC 25", xst.QUICK_MAX_SEC, 25.0),
        ("STEADY_MAX_BYTES 256MiB", xst.STEADY_MAX_BYTES, 256 * 1024 * 1024),
        # v2.3.31: 门槛分组后这里改为钉两张表 —— Cn1 仍是 25(它的记录值在 100 量级),
        # Cn2 是 10(那组的记录区间 21~24, 用 25 等于永久关掉相对劣化档)。
        # 原来这里把 25.0 当字面值钉死, 等于把"错误值"写成了契约。
        ("DEGRADE_MIN_RECORD Cn1=25", wd.DEGRADE_MIN_RECORD.get("Cn1"), 25.0),
        ("DEGRADE_MIN_RECORD Cn2=10", wd.DEGRADE_MIN_RECORD.get("Cn2"), 10.0),
        ("SPEED_MIN_EL 0.1", xst.SPEED_MIN_EL, 0.1),     # 8MiB/0.1s = 80MiB/s 天花板
        ("RETEST_COOLDOWN 3 小时", wd.RETEST_COOLDOWN, 3 * 3600),   # v2.3.15: 冷却必须跨过 cron 周期
    ]
    diffs = ["%s: code=%r" % (n, v) for n, v, exp in pairs if v != exp]
    chk("A4", "代码常量与交付口径一致(README 引用的数字未被偷偷改动)", not diffs, "; ".join(diffs))
    chk("A5", "README 引用的关键常量名都在文档里出现",
        all(k in rd for k in ("MAX_QUICK_CANDS", "QUICK_BUDGET_SEC", "20MiB/s", "55%", "15%", "30%",
                              "8MiB/s", "256MiB", "DoH", "3 小时")),
        "缺: %s" % [k for k in ("MAX_QUICK_CANDS", "QUICK_BUDGET_SEC", "20MiB/s", "55%", "15%", "30%",
                                "8MiB/s", "256MiB", "DoH", "3 小时") if k not in rd])

    vs = set()
    for fn in ("xbox-speedtest.py", "watchdog.py"):
        head = open(os.path.join(d, fn), encoding="utf-8").read().split('"""')[1]
        vs.add(re.search(r"v2\.\d+\.\d+", head).group(0))
    chk("A6", "两脚本版本号一致且 README 已收录", len(vs) == 1 and list(vs)[0] in rd, "versions=%s" % vs)

    # C46: README「文件」清单里写的版本号必须就是当前版本。A6 只做了「当前版本在 README 全文出现过」
    # 这一点包含关系 —— 版本历史里本来就有 v2.3.13, 于是清单停在 v2.3.8 也照样 PASS(实际发生了三个版本)。
    # 注意**不能**改成"全文不得出现旧版本号": 版本历史必须保留 v2.3.7~v2.3.12, 那样写会立刻把正常文档判红。
    # 只圈「## 文件」到下一个「## 」之间的这一节。
    cur = list(vs)[0] if len(vs) == 1 else ""
    man_sec = ""
    if cur:
        i = rd.find("## 文件")
        if i >= 0:
            j = rd.find("\n## ", i + 1)
            man_sec = rd[i:] if j < 0 else rd[i:j]
    man_vs = re.findall(r"v2\.\d+\.\d+", man_sec)
    chk("C46", "README「文件」清单里的版本号 = 当前版本(A6 只查全文包含, 清单写旧了照样绿)",
        bool(cur) and bool(man_vs) and all(v == cur for v in man_vs),
        "清单版本=%s 当前=%s" % (man_vs, cur))

    # 环境变量: 代码读的必须在 README 文档化, 文档写的必须在代码里真的被读
    code = "".join(open(os.path.join(d, f), encoding="utf-8").read() for f in
                   ("xbox-speedtest.py", "watchdog.py", "pool_refresh.py"))
    envs = set(re.findall(r"os\.environ\.get\(\s*[\"']([A-Z_]+)[\"']", code))
    # 审计器自己也读环境变量(XBOX_LEDGER 指向跨目录的台账)。它不在 d 里(变异目录只复制交付物),
    # 所以从审计器自身位置读 —— 否则"文档里写了、代码里真在读"会被报成 ghost, 逼着人把文档删掉。
    # 单独一个变量, 不并进 code: 下面的 A13/A14 是拿 code 找主程序源码特征的。
    _selfsrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit.py"),
                    encoding="utf-8").read()
    envs |= set(re.findall(r"os\.environ\.get\(\s*[\"']([A-Z_]+)[\"']", _selfsrc))
    missing = [e for e in envs if e not in rd]
    ghost = [e for e in re.findall(r"`(XBOX_[A-Z_]+)`", rd) if e not in envs]
    chk("A7", "环境变量代码与文档双向对齐", not missing and not ghost,
        "文档缺=%s 文档多=%s" % (missing, ghost))

    # 死函数: 只在 def 行出现过一次 = 没人调用(交接里最容易误用的就是这种)
    dead = []
    for fn in ("xbox-speedtest.py", "watchdog.py"):
        src = open(os.path.join(d, fn), encoding="utf-8").read()
        tree = ast.parse(src)
        for n in tree.body:
            if isinstance(n, ast.FunctionDef) and n.name not in ("main",):
                if len(re.findall(r"\b%s\b" % re.escape(n.name), src)) <= 1:
                    dead.append("%s:%s" % (fn, n.name))
    chk("A8", "无死函数(lead 被定义却没人调用)", not dead, ",".join(dead))

    unused = []
    for fn in ("xbox-speedtest.py", "watchdog.py", "pool_refresh.py"):
        src = open(os.path.join(d, fn), encoding="utf-8").read()
        tree = ast.parse(src)
        imported = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    imported.add((a.asname or a.name).split(".")[0])
            elif isinstance(n, ast.ImportFrom):
                for a in n.names:
                    imported.add(a.asname or a.name)
        body = src.split('"""', 2)[-1]
        for i in imported:
            if not re.search(r"\b%s\b" % re.escape(i), body):
                unused.append("%s:%s" % (fn, i))
    chk("A9", "无未使用导入", not unused, ",".join(unused))

    chk("A10", "三脚本目录都自定位(无硬编码绝对路径)",
        all('os.path.dirname(os.path.abspath(__file__))' in open(os.path.join(d, f), encoding="utf-8").read()
            for f in ("xbox-speedtest.py", "watchdog.py", "pool_refresh.py")), "")

    # 两个可并行触发的程序必须用不同锁文件, 否则互相误杀
    locks = set(re.findall(r'"(\.\w+\.lock)"', code))
    chk("A11", "两个入口的锁文件互不冲突(且各自存在)", len(locks) == 2, "locks=%s" % locks)

    # 文档不写死断言条数: 一旦写死就会和代码漂移(2026-11-06 实际发生过, README 写 52 实为 53)
    chk("A12", "README 不写死断言/变异体条数(避免文档与审计器漂移)",
        not re.search(r"\d+\s*(?:项断言|个变异体)", rd), str(re.findall(r"\d+\s*(?:项断言|个变异体)", rd)))

    # 日志不许编数字: 候选构成同时用原始 len(top)/len(gd) 与截断后的 len(cands), 会打出 8+31+1≠16
    # (2026-11-06 23:09 NAS 实跑打出 "候选 16 个 (Top8=8 + 本地保送=31 + 当前=1)"), 必须标明口径
    comp = [ln for ln in code.splitlines() if "候选" in ln and "Top" in ln and "print" in ln]
    chk("A13", "候选构成日志标明口径(截断前/截断后), 不得混用两种计数",
        comp and all("截断前" in ln or "截断后" in ln for ln in comp),
        "可疑行=%s" % [ln.strip()[:70] for ln in comp])

    # Range 必须能带起始偏移, 且用格式化拼接: v2.3.5 号称"读到 EOF 续测", 但 Range 写死 bytes=0-167772159,
    # 续测时又把前 160MB 下一遍(2026-11-06 NAS 真机: 两次请求 Range 完全相同, 268MB 流量只覆盖 160MiB)
    chk("A14", "_req 支持 start 偏移且 Range 由 start 拼接(续测不得重下头部)",
        "start=0" in code and "span=RANGE_SPAN" in code
        and re.search(r'"Range":\s*"bytes=%d-%d"\s*%\s*\(start,', code),
        "Range 行=%s" % [ln.strip()[:60] for ln in code.splitlines() if '"Range"' in ln])

    # 台账排序必须与它自己的头部声明一致: 头部写"新条目加在最上面", 而 2026-10-28 00:3x 之后的 25 条
    # (含全部 11-06/11-07 记录)**实际追加在文件底部** —— 只读文件头的人会以为最新停在 11-05,
    # 下一轮审计/接手就按一个过期 48 小时的状态继续改。这正是"文档声称的效果没兑现"里最贵的一种:
    # 它骗的不是某条判断, 而是"当前是什么状态"这个前提。
    # 跨目录(台账在项目上一级), 用 XBOX_LEDGER 指路; 变异自检也靠它注入一份逆序副本。
    # 台账缺失时**直接判红**: 软跳过等于给了"这条以后不会再被检查"的后门。
    led = os.environ.get("XBOX_LEDGER") or os.path.join(
        os.path.dirname(os.path.abspath(d.rstrip("/\\"))), "AI共管台账.md")
    if not os.path.isfile(led):
        chk("A18", "台账条目严格倒序(新条目在最上面)", False,
            "台账不存在: %s(可用 XBOX_LEDGER 指定)" % led)
    else:
        _rows, _nodate = [], []   # _rows: (行号, (日期, 时间), 缩进)
        # 条目判定不能只看顶格 "- "。缩进两格、制表符缩进、星号项目符号都是人正常写 Markdown
        # 会自然写出的形态; 一律 continue 掉等于给"格式漂移"开后门 —— 把最新一条改个前缀
        # 再挪到文件底部, A18 照样全绿(2026-11-07 外部审计实测: 六种写法只有顶格那一种会红)。
        # 所以: 能解析出日期的, 不管前缀长什么样, **都纳入倒序比较**(排序才是这条断言的本体);
        # 形似日期条目但解析不动的(斜杠日期等), 记进 _nodate 判红, 而不是静默跳过。
        # 嵌套的无日期子项照样放过 —— 那不是"想写条目", 只是条目正文里的列表。
        for _ln, _line in enumerate(open(led, encoding="utf-8").read().splitlines(), 1):
            _m = re.match(r"^(\s*)[-*]\s+\*{0,2}(\d{4})-(\d{1,2})-(\d{1,2})"
                          r"(?:[ T]+(\d\d):([0-9x]{2}))?", _line)
            if not _m:
                if re.match(r"^\s*[-*]\s+\*{0,2}\d{4}-\d{1,2}-\d{1,2}", _line):
                    _nodate.append((_ln, "日期条目解析不动: " + _line.strip()[:32]))
                continue
            _key = ("%s-%02d-%02d" % (_m.group(2), int(_m.group(3)), int(_m.group(4))),
                    "99:99" if not _m.group(5) else
                    "%s:%s" % (_m.group(5), _m.group(6).replace("x", "9")))
            _rows.append((_ln, _key, len(_m.group(1).expandtabs(4))))
        # 台账是**平铺**结构: 最浅的那层才是条目, 更深的是子项。子项若以日期开头, 它和
        # 任何条目都没有排序关系(子项记的是"复现于 2025-01-01"这类更早的时间), 混进倒序比较
        # 只会假红; 但也不能静默丢掉(丢掉的子项能灌进条数下限, 掩盖"顶层被砍")。
        # 所以: 单独判红并给出可执行的 why —— 台账条目不要嵌套日期。
        _base = min([r[2] for r in _rows] or [0])
        _flat = [r for r in _rows if r[2] == _base]
        _deep = [(r[0], r[1][0]) for r in _rows if r[2] > _base]
        _inv = [(a[0], a[1], b[1]) for a, b in zip(_flat, _flat[1:]) if a[1] < b[1]]
        # 条数下限: 台账是"只增不改"的载体, 条数实际只会涨。原来写 >=20, 实测把 56 条砍到
        # 最新 20 条仍然全绿 —— 而"头部最新、底下被删空"读起来和真的没区别, 正是 A18 立项要防的
        # 那件事的另一半(它防的是"只读文件头的人拿过期状态当现状")。下限取 50: 当前 56, 留一点
        # 余量给"合并过一条"的正当操作, 但砍掉一个版本的历史就必须红。
        _LEDGER_MIN = 50
        chk("A18", "台账条目严格倒序(新条目在最上面), 且每条都能解析出日期, 且历史没被砍",
            not _inv and not _nodate and not _deep and len(_flat) >= _LEDGER_MIN,
            "逆序=%s 无日期=%s 日期子项=%s 条数=%d(下限 %d)"
            % (_inv[:3], _nodate[:3], _deep[:3], len(_flat), _LEDGER_MIN))

    # A19 引用假账: 文档里点名的检查编号必须真的存在于本审计器。
    # 2026-11-07 实证: 台账头部写着"排序由 audit.py 的 C66 钉住", 而那条检查从落进 A 层起就叫 A18
    # (起草时叫 C66, 改名后头部忘了跟着改)。这比逻辑 bug 更难发现 —— 它**不影响任何行为**,
    # 断言照常全绿, 只让下一个读的人去找一条不存在的检查, 或以为自己漏跑了什么。
    # 扫描面 = 三处"声明当前状态"的地方: 台账**头部声明区**(第一个 "- " 条目之前) /
    # README 的**非历史区**(「## 版本历史」之前) / 两个脚本全文。
    # 两处历史区都**不扫**, 理由相同: 台账条目里记的是外部审计编号(B4/S11/S12)和复盘某个错号,
    # README 版本历史里记的是"当时点名过哪些检查"。那是记账, 不是现状声明。
    # 全扫除了会把"记过去"判成"写假账"(于是下次没人敢如实记录自己写错过什么),
    # 还会让**每一条历史引用变成永久 API**: 将来重构删掉一个旧检查, A19 立刻红,
    # 于是要么保留一个死检查、要么改历史(违背"只增不改")。2026-11-07 外部审计提的正是这一点,
    # 但它判断"这是固有代价"—— 不, 台账那边早就用"只扫头部"解决过同一个问题了。
    # 边界靠标题定位, 所以 A20 盯住那个标题还在(被改名不会报错, 只会让 A19 静默退回扫全文)。
    EXT_IDS = set()     # 外部编号若出现在**当前状态区**(不是历史区)才加这里; 目前为空 = 扫描面里每个编号都真存在
    _real_ids = set(re.findall(r'chk\(\s*"([A-D]\d{1,2}[a-z]?)"', _selfsrc))
    # 段兜底是 chk(cid, ...) 动态传的: A0/B0/C0/D0 在源码里**真实存在**(段调度元组),
    # 但字面量正则看不见 —— 于是文档里如实写"任何一段崩了由 C0 兜住"会被判成假账。
    # 真话被判红和假账被放过是同一个病的两面, 都要堵。模式限定为 "X9") 形态(元组末尾),
    # 抓不到 chk("A19", ...) 那种逗号结尾的, 也不会误收别处的双字符字面量。
    _real_ids |= set(re.findall(r'"([A-D]\d)"\s*\)', _selfsrc))
    # 台账路径必须和 A18 用同一个解析(XBOX_LEDGER 优先) —— 否则变异自检里 d 是临时目录,
    # A19 会去 tmp 的上一级找一个不存在的台账, 于是每条变异都额外红一条 A19(假红, 会掩盖真红)。
    _ledger = os.environ.get("XBOX_LEDGER") or os.path.join(
        os.path.dirname(os.path.abspath(d.rstrip("/\\"))), "AI共管台账.md")
    _ghost = []
    _targets = []       # (报告用的文件名, 已截断的待扫文本)
    for _p, _mode in ((_ledger, "head"),
                      (os.path.join(d, readme_name(d)), "hist"),
                      (os.path.join(d, "xbox-speedtest.py"), "code"),
                      (os.path.join(d, "watchdog.py"), "code")):
        if not os.path.isfile(_p):
            _ghost.append("%s: 文件不存在" % os.path.basename(_p))
            continue
        _t = open(_p, encoding="utf-8").read()
        if _mode == "head":
            _i = _t.find("\n- ")
        elif _mode == "hist":
            _i = _t.find("\n## 版本历史")
        else:
            # 脚本的模块 docstring 是**逐版本追加的变更记录**, 与 README 版本历史同性质:
            # 里面点名"当年那个检查"不该变成永久 API。整段跳过(代码+注释仍扫, 那才是当前状态)。
            # 换行数保留, 行号仍对得上真实文件。
            _i = _t.find('"""')
            if _i >= 0:
                _b = _t.find('"""', _i + 3)
                _t = (_t[:_i] + "\n" * _t.count("\n", _i, _b + 3) + _t[_b + 3:]) if _b > 0 else _t
                _i = -1
        # 截断点被吃掉的那一行换行符不影响行号: 前面几行一字不动
        _targets.append((os.path.basename(_p), _t if _i < 0 else _t[:_i]))
    for _name, _txt in _targets:
        for _ln, _line in enumerate(_txt.splitlines(), 1):
            for _id in re.findall(r"(?<![0-9A-Za-z_])([A-D]\d{1,2}[a-z]?)(?![0-9A-Za-z_])", _line):
                if _id not in _real_ids and _id not in EXT_IDS:
                    _ghost.append("%s:%d -> %s" % (_name, _ln, _id))
    chk("A19", "文档点名的检查编号都真实存在(引用假账: 头部写 C66, 实际叫 A18); 只扫当前状态区",
        not _ghost, "不存在的编号=%s" % _ghost[:6])

    # A20: A19 的扫描边界靠「## 版本历史」标题定位。标题被改名/删掉时 A19 不会报错 ——
    # 它会静默退回"扫全文", 于是 README 里每一条历史引用又变成永久 API(见 A19 注释)。
    # 边界必须是可定位的契约, 不能是"碰巧存在的排版"。
    _rd_p = os.path.join(d, readme_name(d))
    _rd_lines = open(_rd_p, encoding="utf-8").read().splitlines() if os.path.isfile(_rd_p) else []
    _hist_heads = [i for i, l in enumerate(_rd_lines, 1) if l.strip() == "## 版本历史"]
    chk("A20", "README 有且只有一个「## 版本历史」标题 —— A19 的扫描边界(改名会让扫描静默退回全文)",
        len(_hist_heads) == 1, "命中行=%s" % _hist_heads)

    # A21: 自检脚本模块注释里那份"变异清单"必须与实际 MUTANTS 一一对应。
    # 2026-11-07 实证: 清单停在第 M53 条, 而 MUTANTS 里已经有 60 条 —— 少了 M54~M60 七条。
    # 不影响任何行为(自检照样 60/60 全过), 只让下一个读的人以为"变异只到 M53",
    # 看不到 v2.3.18 之后补的那些缝。和 A19 同一类账: 文档声称的清单 ≠ 实际存在的清单。
    _st_p = os.environ.get("XBOX_SELFTEST") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "selftest_audit.py")
    _st_src = open(_st_p, encoding="utf-8").read() if os.path.isfile(_st_p) else ""
    _dm = re.search(r'"""(.*?)"""', _st_src, re.S)
    _listed = set(re.findall(r"(?m)^\s*(M\d+)\s+\S", _dm.group(1) if _dm else ""))
    _actual = set(re.findall(r'\(\s*"(M\d+)"\s*,', _st_src))
    chk("A21", "自检脚本头部那份变异清单 = 实际 MUTANTS(实证: 清单停在 M53, 实际已 60 条)",
        bool(_st_src) and bool(_listed) and _listed == _actual,
        "清单多出=%s 清单漏掉=%s" % (sorted(_listed - _actual), sorted(_actual - _listed)))

    # A22: py3.8 语法/API 兼容。NAS 跑 3.8.10, 开发机是 3.13 —— "本地全绿、NAS 上炸" 这条路
    # 目前只是**碰巧**没走通: 没有任何断言钉着它。2026-11-07 外部审计手工验过一次
    # (五个文件 ast.parse(feature_version=(3,8)) 全过 + 新版 API 零命中), 但手验一次的结论会腐烂,
    # 变成断言才会一直真 —— 而且它把"必须上机才知道"降级成"本地跑一遍就知道"。
    # 两半各管一类: ast 抓 3.9+ **语法**(match 语句等), 文本扫抓 3.9+ **API**(语法合法但 3.8 没有)。
    _py38_bad = []
    # 三个运行时文件跟着 --dir 走(变异测试要能改它们); 审计器自己那两个必须指向**真实路径** ——
    # 变异目录只复制 FILES 那四个, tests/ 不在里面, 按 d 找会全部"文件不存在"而假红。
    _self_dir = os.path.dirname(os.path.abspath(__file__))
    _py38_st = os.environ.get("XBOX_SELFTEST") or os.path.join(_self_dir, "selftest_audit.py")
    # v2.3.31: `tests/replay.py` 也进 NAS 侧交付(README 清单里有它), 所以它同样得被 3.8 兼容钉住。
    # 走**真实路径**而不是 `d`: 变异目录只复制 FILES 那几个, tests/ 不在里面 —— 按 d 找会
    # 「文件不存在」, 于是每个变异体都红在 A22, 等于这条断言被所有变异体"抓到", 也就没意义了。
    for _f, _p in (("xbox-speedtest.py", os.path.join(d, "xbox-speedtest.py")),
                   ("watchdog.py", os.path.join(d, "watchdog.py")),
                   ("pool_refresh.py", os.path.join(d, "pool_refresh.py")),
                   ("tests/audit.py", os.path.abspath(__file__)),
                   ("tests/replay.py", os.path.join(_self_dir, "replay.py")),
                   ("tests/selftest_audit.py", _py38_st)):
        if not os.path.isfile(_p):
            _py38_bad.append("%s: 文件不存在" % _f)
            continue
        _s = open(_p, encoding="utf-8").read()
        try:
            _tree = ast.parse(_s, filename=_f, feature_version=(3, 8))
        except SyntaxError as _e:
            _py38_bad.append("%s: 3.9+ 语法 -> %s" % (_f, _e.msg))
        else:
            # PEP 585 内置泛型下标: 语法在 3.8 合法(就是个 Subscript), ast 拦不住, 但
            # def/模块级注解在 3.8 会被**求值** -> TypeError: 'type' object is not subscriptable。
            # 与 removeprefix 同一类"语法合法、运行时炸", 只是换成注解形态。
            for _nd in ast.walk(_tree):
                if (isinstance(_nd, ast.Subscript) and isinstance(_nd.value, ast.Name)
                        and _nd.value.id in ("list", "dict", "tuple", "set", "frozenset", "type")):
                    _py38_bad.append("%s:%d -> 3.8 不支持内置泛型下标 %s[...]"
                                     % (_f, _nd.lineno, _nd.value.id))
        for _mm in re.finditer(r"\.(?:removeprefix|removesuffix)\(|functools\.cache\b(?!_)|"
                               r"^\s*(?:from|import)\s+zoneinfo", _s, re.M):
            _py38_bad.append("%s:%d -> %s" % (
                _f, _s.count("\n", 0, _mm.start()) + 1, _mm.group(0).strip()[:24]))
        # 注解里的 `X 竖线 None`: PEP 604 属于 3.10, 3.8 定义期即 TypeError。
        # feature_version=(3,8) 拦不住它 —— 语法上就是个 BitOr BinOp(实测 3.13 的 ast 照收)。
        # **只能看注解位置**: 全文正则会把注释/字符串里写的 `int 竖线 None` 判红(2026-11-07 实测假红),
        # 而假红会挡掉反证——把真注解写进变异清单时, A22 扫的是 selftest 的真实路径,
        # 变异串自己就被当代码判红, 于是这半边永久没有变异钉住。宽断言不会让检查更严, 它让检查无法被证明。
        for _nd in ast.walk(_tree):
            _anns = []
            if isinstance(_nd, ast.arg) and _nd.annotation is not None:
                _anns.append(_nd.annotation)
            elif isinstance(_nd, (ast.FunctionDef, ast.AsyncFunctionDef)) and _nd.returns is not None:
                _anns.append(_nd.returns)
            elif isinstance(_nd, ast.AnnAssign):
                _anns.append(_nd.annotation)
            for _a in _anns:
                for _b in ast.walk(_a):
                    if not (isinstance(_b, ast.BinOp) and isinstance(_b.op, ast.BitOr)):
                        continue
                    if any((isinstance(_x, ast.Constant) and _x.value is None)
                           or (isinstance(_x, ast.Name) and _x.id == "None")
                           for _x in (_b.left, _b.right)):
                        _py38_bad.append("%s:%d -> 注解用 竖线 None(3.10+ 语法, 3.8 定义期即 TypeError)"
                                         % (_f, _b.lineno))
    chk("A22", "py3.8 兼容(NAS=3.8.10 / 本地=3.13): 语法与 3.9+ API 都在本地钉住, 不靠上机才发现",
        not _py38_bad, "不兼容=%s" % _py38_bad[:6])

    # 两边窗口口径必须同量级, 否则 results.json 里的记录值与 watchdog 实测值不可比,
    # DEGRADE_RATIO 的判定会系统性偏移(真机: 同节点 101.4 vs 96.4, 差 5%)
    xst_bs = load(os.path.join(d, "xbox-speedtest.py"), "xst_bs")
    wdm_bs = load(os.path.join(d, "watchdog.py"), "wdm_bs")
    chk("A15", "watchdog 与 speedtest 的稳态字节上限一致(记录值才可比)",
        getattr(wdm_bs, "STEADY_MAX_BYTES", None) == getattr(xst_bs, "STEADY_MAX_BYTES", -1)
        and getattr(xst_bs, "STEADY_MAX_BYTES", 0) > 0,
        "speedtest=%s watchdog=%s" % (getattr(xst_bs, "STEADY_MAX_BYTES", None),
                                      getattr(wdm_bs, "STEADY_MAX_BYTES", None)))

    # 热备候选数跟着名单容量走: 写死 5 时每组 5×3 轮×256MiB=3.8GiB, 一轮全量网卡实测 9.58GiB
    chk("A16", "稳态候选数由 HOTSPARE_N 推导(不写死), 且不超过名单容量+1",
        getattr(xst_bs, "STEADY_CAND_MAX", None) == xst_bs.HOTSPARE_N + 1,
        "STEADY_CAND_MAX=%s HOTSPARE_N=%s" % (getattr(xst_bs, "STEADY_CAND_MAX", None),
                                              getattr(xst_bs, "HOTSPARE_N", None)))

    # 计时起点必须在首次响应头到达之后: 把 TCP 建连算进窗口会系统性低估
    # (2026-11-06 真机 dl_steady 92.10 vs watchdog 99.90, 差 7.8%, 根因就是 t0 在 connect 之前)
    fn_steady = code.split("def dl_steady", 1)[1].split("\ndef ", 1)[0]
    i_resp = fn_steady.find("resp = conn.getresponse()")
    i_t0 = fn_steady.find("if t0 is None:")
    chk("A17", "稳态计时起点延迟到首次响应头之后(TCP 建连不算吞吐)",
        "t0 = None" in fn_steady and i_resp >= 0 and i_t0 > i_resp,
        "resp@%d t0@%d" % (i_resp, i_t0))

    # A23: README 把常量数值当**论据**(不止是举例), 却没有任何断言把那个数字和代码连起来。
    # 2026-11-07 实测: RTT_TOP 48 -> 72, 137 项全绿, 而 README 还在用 48 推「48 刚好不漏」。
    # 数字腐烂比措辞腐烂危险 —— 措辞错了读起来别扭, 数字错了句子照样通顺。
    # 边界与 A19 同一条: 只扫「## 版本历史」之前。历史里 RETEST_TIMEOUT=5400 之类是当时的记账,
    # 拿历史判现状就是拿记账当代码。dict 类阈值(WIN_RATIO={"Cn1":1.15,...})本正则不碰 ——
    # 覆盖率只写能兑现的那部分, 不假装盖住。
    _consts = {}
    for _mod in (xst_bs, wdm_bs, load(os.path.join(d, "pool_refresh.py"), "prf_a23")):
        for _k, _v in vars(_mod).items():
            if _k.isupper() and isinstance(_v, (int, float)) and not isinstance(_v, bool):
                _consts[_k] = float(_v)
    _rdt = open(os.path.join(d, readme_name(d)), encoding="utf-8").read()
    _rhi = _rdt.find("\n## 版本历史")
    if _rhi > 0:
        _rdt = _rdt[:_rhi]
    _drift = []
    for _mm in re.finditer(r"\b([A-Z][A-Z0-9_]{3,})\s*=\s*(\d+(?:\.\d+)?)\b", _rdt):
        _k, _v = _mm.group(1), float(_mm.group(2))
        if _k in _consts and abs(_consts[_k] - _v) > 1e-9:
            _drift.append("%s README=%s 代码=%g" % (_k, _mm.group(2), _consts[_k]))
    chk("A23", "README 里 `常量=数值` 必须等于代码真值(dict 类阈值不在本断言范围)",
        not _drift, "漂移=%s" % _drift[:6])

    # A24: samples.jsonl —— 闭环样本。results.json 只留**结论**(冠军+热备), 一轮几百个测量值
    # 原本全丢在 stdout 里: WIN_RATIO/REPLACE_MIN/DEGRADE_RATIO 只有 n=1 的依据, watchdog 判
    # degraded 也只能拿一个数跟单次实测比(同节点单次实测差 4 倍)。这条钉七件, 每件都有反证:
    #   (a) build_sample 是纯函数且字段齐全 —— M81 把 quick 表删空
    #   (b) 落盘是 append —— M82 改成 "w"(覆盖 = 当天分布只剩最后一轮)
    #   (c) 样本不进 results.json —— M83 把 "_sample" 挂回返回值(它会跟着 r 进 results.json)
    #   (d) 路径调用时用 BASE 拼 —— M88 模块常量 / M89 用 __file__ 绕开 BASE
    #   (e) 样本带**判定输入** st_cand/st_cur/wr —— 否则回放只能正则抠 router 的散文
    #   (f) 坏日子(快测全 0)仍留 1 行且仍返回 None —— M90 把 sink 判断写反
    #   (g) 截断尾部与池子计数真的被填 —— M92 删 tail.extend / M93 删 stats.update
    # 纯函数那部分是真调用, 不是数文本: 取整/标签去空白都在语义里, 改一处就该红。
    _xs = open(os.path.join(d, "xbox-speedtest.py"), encoding="utf-8").read()
    _a24 = []
    _bs = getattr(xst_bs, "build_sample", None)
    if not callable(_bs):
        _a24.append("build_sample 不存在")
    else:
        try:
            _row = _bs("Cn1", 6.2, [(11.0, "1.1.1.1", " 广东电信 "), (9.5, "2.2.2.2", "")],
                       [(103.6, "1.1.1.1")], {"1.1.1.1": 103.57}, {"1.1.1.1": " 广东电信 "},
                       [[19.3, "9.9.9.9"], [20.1, "9.9.9.8"]], {"pool": 274, "probed": 274, "alive": 60},
                       screen_capped=["8.8.8.8"], quick_skip=["7.7.7.7"])
        except Exception as _e:
            _row = None
            _a24.append("build_sample 调用失败: %s" % _e)
        if isinstance(_row, dict):
            _miss = [k for k in ("ts", "group", "l0", "screen", "quick", "t_l0",
                                 "l0_tail", "l0_pool", "l0_probed", "l0_alive",
                                 "screen_capped", "quick_skip") if k not in _row]
            if _miss:
                _a24.append("缺字段 %s" % _miss)
            _l0 = _row.get("l0") or []
            # 这条**不**证明 l0 是升序的 —— 夹具故意给降序 (11.0, 9.5), 断言要求"原样保留",
            # 因此它证明的是「build_sample 不重排」。排序本身由 C68 钉(拿真 rtt_rank 跑)。
            # v2.3.29 之前这里注释写的是"顺序是语义: l0 按 ms 升序", 比断言说的多 —— 那种注释
            # 会让下一个人以为删掉 sorted 会被 A24 抓住, 而实际抓它的是 C68。
            if [r[1] for r in _l0[:2]] != ["1.1.1.1", "2.2.2.2"]:
                _a24.append("l0 被重排/为空(断言只要求原样保留, 排序归 C68): %r" % (_l0[:2],))
            elif _l0[0][2] != "广东电信":
                _a24.append("l0 标签没去空白: %r" % (_l0[0],))
            # v2.3.31: 尾部必须**带 IP**。旧断言只要求长度是 2(裸 ms), 那证明不了"第 49 名是谁";
            # 而"漏了个更近的节点"这件事, 恰恰只能靠 IP 去补测/去论证 48 该不该调大。
            if _row.get("l0_tail") != [[19.3, "9.9.9.9"], [20.1, "9.9.9.8"]]:
                _a24.append("l0_tail 丢了 IP 或被改写: %r(裸 ms 回答不了『漏的是谁』)"
                            % (_row.get("l0_tail"),))
            _st24 = (_row.get("l0_pool"), _row.get("l0_probed"), _row.get("l0_alive"))
            if _st24 != (274, 274, 60):
                _a24.append("l0_pool/probed/alive 没落进样本: %r" % (_st24,))
            if _row.get("screen_capped") != ["8.8.8.8"]:
                _a24.append("screen_capped 没落进样本: %r(80.0 是下界还是精确值就分不清了)"
                            % (_row.get("screen_capped"),))
            if _row.get("quick_skip") != ["7.7.7.7"]:
                _a24.append("quick_skip 没落进样本: %r(『预算跳过』与『测了是 0』同形)"
                            % (_row.get("quick_skip"),))
            _q = (_row.get("quick") or {}).get("1.1.1.1")
            if not isinstance(_q, (int, float)) or abs(_q - 103.6) > 1e-9:
                _a24.append("quick 丢了或没取整: %r" % (_row.get("quick"),))
        elif _row is not None:
            _a24.append("build_sample 返回值不是 dict")
    if 'open(_path, "a"' not in _xs:
        _a24.append('落盘不是 append 形态 open(_path, "a")')
    # 样本走 sink 参数直接进 samples 列表, 不塞进 test_pool 的返回值 —— 塞进去就得把
    # `return None` 改成 dict, 而那是这条链上唯一的失败出口(B1 棘轮钉着它)。
    if '"_sample"' in _xs:
        _a24.append('"_sample" 又挂在返回值上了(要么漏进 results.json, 要么把失败出口改成成功形态)')
    if "sample_sink" not in _xs:
        _a24.append("test_pool 没接 sample_sink: 无冠军那几轮不会留下样本")
    # (e) 样本要能回答"阈值该不该调", 就必须带判定输入(st_cand 对 st_cur*wr)。
    #     只有结论文案的话, 回放得先用正则去抠「kept (steady 103.6 < cur 87.9*1.3)」,
    #     而切换分支的文案里一个数都没有(写的是 assets1=IP 的 detail)。
    if "_dec.get(" not in _xs or '"st_cand"' not in _xs:
        _a24.append("样本没并判定输入(st_cand/st_cur/wr): 回放只能靠正则解析 decision 文案")
    # (f)(g) 行为面: 全组 0 分那轮必须 (i) 仍返回 None (ii) 仍留 1 行样本;
    #       真 rtt_rank 必须真的把 tail/stats 填出来。
    try:
        _x24 = load(os.path.join(d, "xbox-speedtest.py"), "xst_a24nw")
        _x24.rtt_rank = lambda rows, **k: [(10.0, "1.1.1.1", "广东")]
        # v2.3.31: 生产调用点多了 ms_of/capped_out 两个关键字, 桩必须接住(**k) ——
        # 不接就是 TypeError, 而它会被 A24 的 except 兜成"样本段失败", 看起来像样本坏了。
        _x24._screen = lambda rows, h, p, **k: [(50.0, "1.1.1.1")]
        _x24.median_quick = lambda ip, h, p: 0.0
        _x24.load_ips = lambda p: [("1.1.1.1", "广东")]
        _sink24 = []
        _rn24, _ = capture(_x24.test_pool, "Cn1", "x", "h", "/u", "9.9.9.9", _sink24)
        if _rn24 is not None:
            _a24.append("快测全 0 时返回值不再是 None(失败出口被改成了成功形态)")
        if len(_sink24) != 1 or _sink24[0].get("quick", {}).get("1.1.1.1") != 0.0:
            _a24.append("全 0 那轮没留下样本: %r" % (_sink24,))
        _x24t = load(os.path.join(d, "xbox-speedtest.py"), "xst_a24tail")
        _ms24 = {"1.1.1.1": 11.0, "2.2.2.2": 12.0, "3.3.3.3": 13.0, "4.4.4.4": 14.0}
        _x24t._rtt_min = lambda ip, port=443: _ms24[ip]
        _t24 = []
        _s24 = {}
        # load() 会把 rtt_rank 换成离线桩, 所以这里必须取 _rtt_rank_real(真实现)。
        _top24 = _x24t._rtt_rank_real([("1.1.1.1", ""), ("2.2.2.2", ""), ("3.3.3.3", ""),
                                       ("4.4.4.4", "")], top=2, tail=_t24, stats=_s24)
        if [r[1] for r in _top24] != ["1.1.1.1", "2.2.2.2"] or _t24 != [[13.0, "3.3.3.3"], [14.0, "4.4.4.4"]]:
            _a24.append("rtt_rank 没交出被截断的尾部(必须是 [ms, ip]): top=%r tail=%r"
                        % ([r[1] for r in _top24], _t24))
        if _s24 != {"pool": 4, "probed": 4, "alive": 4}:
            _a24.append("rtt_rank 没交代池子大小/实发/通数: %r" % (_s24,))
    except Exception as _e24:
        _a24.append("无冠军样本/截断尾部实测失败: %s" % _e24)
    # (d) 路径必须**调用时**用 BASE 拼。v2.3.28 写成模块级常量, 于是审计里 st_run 改了 x.BASE
    #     也重定向不动它 —— 一次 audit 38 个用例各 append 一行假样本进真文件(2026-11-07 实测)。
    #     口径同 RETEST_MARK_NAME: 「路径在调用时用 BASE 拼, 模块导入时算死的话, 测试改 BASE 就
    #     重定向不动」。这里看 AST 不看正文: 注释里写一句 `SAMPLES = join(BASE,...)` 不该判红
    #     (v2.3.27 A22 栽过 —— 全文正则把注释当代码)。
    if 'os.path.join(BASE, "samples.jsonl")' not in _xs:
        _a24.append('样本路径不是调用时 BASE 拼的 os.path.join(BASE, "samples.jsonl")')
    try:
        _tree24 = ast.parse(_xs)
        # 只认一种形态: 模块体里的赋值语句, 右边出现 "samples.jsonl" 路径
        _mod24 = [n for n in _tree24.body
                  if isinstance(n, (ast.Assign, ast.AnnAssign)) and "samples.jsonl" in ast.dump(n)]
        if _mod24:
            _a24.append("模块级又出现了 samples 路径常量(改 BASE 重定向不动, 会污染真文件)")
    except SyntaxError:
        _a24.append("源码不可解析")
    chk("A24", "samples.jsonl: 每轮每组 append 一行闭环样本(L0+截断尾部+池子计数/粗筛/快测/计时"
               "/判定输入), 坏日子也留一行, 且决策一次都不读它",
        not _a24, "; ".join(_a24))

    # A25: 版本化 URL canary。Cn2 的 TESTPATH 带 build 号, 一撤, **测速与看门狗用的是同一个 URL**,
    # 同一时刻一起瞎 —— 日志只剩 steady=0.0, 读起来像"网烂了"。canary 买不到"提前 N 天"
    # (CDN 有没有预警期一次都没观测过), 买的是**点名**。四件, 各有反证:
    #   走 DNS 不绑 IP(M84 的假警报比不报更有害) / 只发 HEAD / 200 与 206 都算活 / 真的被调用。
    _ws = open(os.path.join(d, "watchdog.py"), encoding="utf-8").read()
    _a25 = []
    if "def canary(" not in _ws:
        _a25.append("canary 不存在")
    if 'conn.request("HEAD"' not in _ws:
        _a25.append("不是 HEAD")
    _cbody = _ws.split("def canary(", 1)[-1].split("\ndef ", 1)[0]
    if "BindIPConn(" in _cbody:
        _a25.append("canary 里绑了 IP: 单台节点坏了会伪装成 URL 下架")
    # 明文 HTTP, 与下载路径同口径。2026-11-07 18:46 NAS 实测: 一开始写的是 HTTPSConnection,
    # 真 URL 直接 SSLCertVerificationError(Hostname mismatch, 证书不是给 dlassets.xboxlive.cn 签的)
    # —— 而下载走的是 HTTP + Host 头, 从来没做过 TLS 校验。等于给体检加了一道被体检对象不遵守的门,
    # 每小时报一次假警。这条不写下来, 下一个人会觉得"HTTPS 更安全"再改回去。
    if "HTTPSConnection(" in _cbody or "HTTPConnection(" not in _cbody:
        _a25.append("canary 用了 HTTPS: 下载路径是明文 HTTP, 真机实测真 URL 会 SSLCertVerificationError")
    if "st in (200, 206)" not in _cbody:
        _a25.append("没把 206 算活: 正常返回会被每小时误报成下架")
    # v2.3.31: 调用形态变了 —— main 现在走 `canary_gate()`, 它才是把档位变成决策的入口。
    # 这里不能再钉 `for _cn in CANARY: canary(_cn)`(那正是审查方 P1-2 指出的"只点名"写法),
    # 改成: ①main 必须调用 canary_gate; ②canary_gate 必须真的把 state 交给 canary。
    # 行为面("gate 三档对不对")由 C73 钉, 这条只管"接上了没有"。
    # **必须用 ast 不能用文本**: 本文件的版本头注释里就写着 `canary_gate()` 两处 —— 用
    # `"canary_gate()" in _ws` 判, 变异把 main 那行删掉之后注释仍然满足它, A25 照样绿
    # (M84 的实测: 删掉调用线, 文本口径 rc=0)。注释不是调用。
    _wt = None
    try:
        _wt = ast.parse(_ws)
    except SyntaxError:
        _a25.append("watchdog.py 不可解析, 查不出 canary 有没有进决策")

    def _calls_in(fname, callee, kw=None):
        """fname 的函数体里有没有对 callee 的调用(可选: 必须带名为 kw 的关键字参数)"""
        for _fn in (_wt.body if _wt else []):
            if isinstance(_fn, ast.FunctionDef) and _fn.name == fname:
                for _n in ast.walk(_fn):
                    if (isinstance(_n, ast.Call) and isinstance(_n.func, ast.Name)
                            and _n.func.id == callee):
                        if kw is None or any(_k.arg == kw for _k in _n.keywords):
                            return True
        return False

    if not _calls_in("main", "canary_gate"):
        _a25.append("main 里没有真的调用 canary_gate(注释里提到不算): 档位再准也进不了决策")
    if not _calls_in("canary_gate", "canary", "state"):
        _a25.append("canary_gate 没有把 state 交给 canary(闸门不知道每组的档位)")
    chk("A25", "版本化 URL canary: 每小时 HEAD 一次, 走 DNS 不绑 IP, 200/206 算活, "
               "且返回值经 canary_gate 进决策",
        not _a25, "; ".join(_a25))


# ------------------------------------------------------------ B 失败出口棘轮
def is_fail_return(n):
    """失败出口: `return <常数>`、`return (False, "原因")`、`return x if cond else None`。
    本项目里 router_apply 这类函数一律用 (bool, detail) 成对返回, 只认裸常数会让它们
    在棘轮里隐形 —— 2026-11-06 NAS 实跑后发现主程序 router_apply 的 restart 失败分支
    根本没被 B1 看见(watchdog 那边修了, 主程序漏了), 就是这条盲区的直接后果。
    v2.3.19: 补 `ast.IfExp`(条件表达式)。**这一条比 Tuple 那条更隐蔽**: `return x if ok else None`
    在源码里长得像"正常返回", 而 `None` 恰恰是所有 `is None` 判定的落点 —— 两个 router_cmd
    (`return r.stdout.strip() if r.returncode == 0 else None`) 一个都没被数进去, 等于棘轮对
    "ssh 失败" 这条最关键的路径完全失明。把 `else None` 删掉、直接 `return r.stdout.strip()`,
    rc!=0 就返回 "" 而不是 None, 全部 `is None` 判定失效(router_apply 认为 uci 成功、quick_fix
    把失败当成功), 而 B1 照样 PASS、55 个变异体全过 —— 这就是补这条的理由。
    仍然不认 `break`/`continue`(控制流不是返回值) 和 `return float(x or 0.0)`(Call 内部
    `or` 的语义静态判断不了, 硬猜会把 TypeError 兜底也算成失败出口, 噪声大于收益)。"""
    if not isinstance(n, ast.Return) or n.value is None:
        return False
    v = n.value
    if isinstance(v, ast.Constant):
        return v.value in FAIL_CONSTS
    if isinstance(v, ast.Tuple) and v.elts:
        e0 = v.elts[0]
        return isinstance(e0, ast.Constant) and e0.value in FAIL_CONSTS
    if isinstance(v, ast.IfExp):
        # 两个分支任一是失败常数就算: `x if ok else None` 与 `None if bad else x` 是同一个洞
        return (is_fail_return(ast.Return(value=v.body))
                or is_fail_return(ast.Return(value=v.orelse)))
    return False


def collect_failure_exits(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    counts = {}

    def walk(node, scope):
        for n in ast.iter_child_nodes(node):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                counts.setdefault(n.name, 0)
                walk(n, n.name)
            else:
                if is_fail_return(n):
                    counts[scope] = counts.get(scope, 0) + 1
                elif isinstance(n, ast.ExceptHandler):
                    counts[scope] = counts.get(scope, 0) + 1
                walk(n, scope)

    walk(tree, "<module>")
    return counts


def audit_failure_registry(d):
    print("\n--- B 失败出口棘轮 ---")
    actual = {}
    for fn in ("xbox-speedtest.py", "watchdog.py", "pool_refresh.py"):
        for k, v in collect_failure_exits(os.path.join(d, fn)).items():
            if v:
                actual[(fn, k)] = v
    new = sorted(set(actual) - set(FAILURE_EXITS))
    gone = sorted(set(FAILURE_EXITS) - set(actual))
    shifted = sorted(k for k in set(actual) & set(FAILURE_EXITS) if actual[k] != FAILURE_EXITS[k])
    chk("B1", "失败出口数量与登记表一致(新增/删除失败路径会被拦下)", not (new or gone or shifted),
        "未登记=%s 已消失=%s 数量变了=%s " % (new, gone, [(k, FAILURE_EXITS[k], actual[k]) for k in shifted]))

    untested = [k for k in actual if k not in FAILURE_TESTS]
    chk("B2", "每个含失败出口的函数都挂了用例名", not untested, "未挂=%s" % untested)

    # 用例名必须真的存在(防止登记表写成好看的假名字)
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    ghost = []
    for k, ids in FAILURE_TESTS.items():
        for cid in ids:
            if ('chk("%s"' % cid) not in src:
                ghost.append("%s->%s" % (k[1], cid))
    chk("B3", "登记表引用的用例名都真实存在", not ghost, "幽灵用例=%s" % ghost)
    return actual


# --------------------------------------------------------------- C 行为契约
def audit_behaviour(d):
    print("\n--- C 行为契约 ---")
    xs_p = os.path.join(d, "xbox-speedtest.py")
    wd_p = os.path.join(d, "watchdog.py")

    # ---- C1 分类边界 ----
    wd = load(wd_p, "wd")
    # (实测, 记录值, 期望档): 8.0 已过绝对阈值但因掉到记录 55% 以下仍判 degraded;
    # 记录值本身 <25 时不做相对判定(本就不快的节点别抖)
    cases = [(0.0, 100.0, "dead"), (7.9, 100.0, "slow"), (8.0, 100.0, "degraded"),
             (54.9, 100.0, "degraded"), (56.0, 100.0, "ok"), (10.0, 20.0, "ok"),
             (90.0, 100.0, "ok"), (30.0, 0.0, "ok"), (0.0, 0.0, "dead")]
    bad = [(s, r, wd.classify(s, r)[0]) for s, r, exp in cases if wd.classify(s, r)[0] != exp]
    chk("C1", "classify 五档边界正确(dead/slow/degraded/ok, 记录值<25 不做相对判定)",
        not bad, "错档=%s" % bad)

    # ---- C2 单次采样抖动不得误判 ----
    st = _stub_wd(wd_p)
    st.set_pins({"Cn1": "9.9.9.9", "Cn2": "8.8.8.8"})
    st.seq("9.9.9.9", [10.6, 90.0])       # 首测低、复测高 => 应判 ok
    st.run()
    chk("C2", "首测 10.6 复测 90 判 ok 且不触发重测",
        "=> ok" in st.out and "[trigger]" not in st.out, st.out[-300:])

    st = _stub_wd(wd_p)
    st.set_pins({"Cn1": "9.9.9.9", "Cn2": "8.8.8.8"})
    st.seq("9.9.9.9", [10.6, 10.0])
    st.run()
    chk("C2b", "两次都低才判 degraded 并触发定向重测",
        "degraded" in st.out and "--pool Cn1" in st.out, st.out[-300:])

    # ---- C62 触发只剩 [trigger]、没有结果(旧实现不设 timeout 也不看 returncode) ----
    # 定向重测是本程序唯一花钱的动作(单组 3.71GiB)。子进程被 OOM 杀掉 / 抢锁失败早退 / 跑完了,
    # 三者都必须能在日志里分开, 否则"重测过了没用"与"重测压根没跑"无法区分。
    st = _stub_wd(wd_p)
    st.set_pins({"Cn1": "9.9.9.9", "Cn2": "8.8.8.8"})
    st.seq("9.9.9.9", [1.0, 1.0])
    st.run()
    kw = dict(st.ran[0][1]) if st.ran else {}
    chk("C62", "触发定向重测: 子进程带 timeout, 且退出码进日志(桩返回 3)",
        bool(st.ran) and "timeout" in kw and "[retest]" in st.out and "退出码 3" in st.out,
        "跑了 %d 次 kw=%s tail=%r" % (len(st.ran), sorted(kw), st.out[-160:]))

    # ---- C61 空 uci 配置 != 路由器不可达 ----
    # `uci show dhcp | grep address` 在没有任何 address 项的路由器上退 1(本机复现:
    # printf "" | grep address -> rc=1), router_cmd 把非 0 折成 None, 于是"空配置"和"ssh 不通"
    # 变成同一个 None -> 整轮 return, 日志说"ssh 不可达"。换新路由器/配置被清空后永久空转。
    def run_xs_main(current_value):
        x = load(xs_p, "xst61")
        x.BASE = tempfile.mkdtemp()
        x.POOLS = []                 # 环境自检段不需要真去测速
        x.test_pool = lambda *a, **k: None
        calls = []

        def router_cmd(cmd):
            calls.append(cmd)
            return current_value
        x.router_cmd = router_cmd
        old = sys.argv
        sys.argv = ["xbox-speedtest.py"]
        try:
            _r, out = capture(x.main)
        finally:
            sys.argv = old
        return out, calls

    out_none, _c = run_xs_main(None)
    out_empty, calls_empty = run_xs_main("")
    chk("C61", "空 uci 配置(ssh 通、一条 address 都没有)不再被当成路由器不可达而整轮跳过",
        "ssh 不可达" in out_none and "ssh 不可达" not in out_empty and "空配置" in out_empty,
        "None轮=%r / 空配置轮=%r" % (out_none[-120:], out_empty[-160:]))
    # 光有行为断言挡不住"把 || true 删掉": 桩里 router_cmd 不真的走 ssh。命令串必须自己容忍
    # grep 的空匹配(rc=1) —— 这是唯一能在这个仓里 encode 退出码语义的地方。
    chk("C61b", "读 uci 的命令串容忍 grep 空匹配(含 || true), 否则干净路由器上 rc=1 必被折成 None",
        bool(calls_empty) and "|| true" in calls_empty[0], "cmd=%r" % (calls_empty[:1],))

    # ---- C3..C6 热备止血 ----
    def wd_fresh(hist, hotspares, steady_map, apply_result=None):
        m = load(wd_p, "wd")
        m.BASE = tempfile.mkdtemp()
        rec = []

        def router_cmd(cmd):
            rec.append(cmd)
            if "show" in cmd:
                return hist
            if apply_result is None:
                return ""
            return apply_result(cmd)
        m.router_cmd = router_cmd
        m.load_hotspares = lambda n: hotspares
        m.steady = lambda ip, host=None, path=None: steady_map.get(ip, 0.0)
        return m, rec

    hist4 = ("dhcp.@dnsmasq[0].address='/assets1.xboxlive.cn/1.2.3.4' "
             "'/assets2.xboxlive.cn/1.2.3.4' '/d1.xboxlive.cn/1.2.3.4' '/d2.xboxlive.cn/1.2.3.4'")
    m, rec = wd_fresh(hist4, [{"ip": "7.7.7.7", "steady": 99}], {"7.7.7.7": 99.0})
    ip, out = capture(m.quick_fix, "Cn1", "1.2.3.4")
    blob = " ".join(rec)
    chk("C3", "止血一次切整组 4 个域名(不是只切 assets1)",
        ip == "7.7.7.7" and blob.count("add_list") == 4 and blob.count("del_list") == 4,
        "ip=%s add=%d del=%d" % (ip, blob.count("add_list"), blob.count("del_list")))
    chk("C3b", "止血日志写明切的域名个数", "4 个域名" in out, out[-200:])

    m, rec = wd_fresh(hist4, [{"ip": "1.2.3.4", "steady": 99}, {"ip": "8.8.8.8", "steady": 90}],
                      {"1.2.3.4": 99.0, "8.8.8.8": 90.0})
    ip = capture(m.quick_fix, "Cn1", "1.2.3.4")[0]
    chk("C4", "跳过与当前 IP 相同的热备项", ip == "8.8.8.8", "ip=%s" % ip)

    m, rec = wd_fresh(hist4, [{"ip": "7.7.7.7", "steady": 4.3}], {"7.7.7.7": 4.3})
    ip = capture(m.quick_fix, "Cn1", "1.2.3.4")[0]
    chk("C5", "低于 20MiB/s 门槛的热备不切(返回 None)", ip is None, "ip=%s" % ip)

    # 只让"第一次" uci 写失败, 第二次必须成功 —— 验的是"失败后换下一个热备", 不是"永远失败"
    state = {"failed": False}

    def fail_uci(cmd):
        if "del_list" in cmd and not state["failed"]:
            state["failed"] = True
            return None
        return ""
    m, rec = wd_fresh(hist4, [{"ip": "7.7.7.7", "steady": 99}, {"ip": "8.8.8.8", "steady": 90}],
                      {"7.7.7.7": 99.0, "8.8.8.8": 90.0}, apply_result=fail_uci)
    ip = capture(m.quick_fix, "Cn1", "1.2.3.4")[0]
    chk("C6", "uci 写入失败时换下一个热备(不谎报成功)", ip == "8.8.8.8", "ip=%s" % ip)

    # ---- C7..C10 router_apply ----
    m = load(wd_p, "wd")
    calls = []

    def rc_show(cmd):
        calls.append(cmd)
        return hist4
    m.router_cmd = rc_show
    ok, _out = capture(m.router_apply, list(m.DOMAINS["Cn1"]), "1.2.3.4")
    chk("C7", "全部已是最新时幂等返回 True 且不写路由器",
        ok is True and len(calls) == 1, "calls=%d" % len(calls))

    hist2 = ("dhcp.@dnsmasq[0].address='/d1.xboxlive.cn/9.9.9.9' '/d1.xboxlive.cn/8.8.8.8' "
             "'/d2.xboxlive.cn/9.9.9.9'")
    st2 = []
    m = load(wd_p, "wd")
    m.router_cmd = lambda cmd: (hist2 if "show" in cmd else st2.append(cmd) or "")
    ok, _o = capture(m.router_apply, ["d1.xboxlive.cn", "d2.xboxlive.cn"], "1.1.1.1")
    blob = " ".join(st2)
    chk("C8", "多历史值全部 del 后再 add(防 address 堆积)",
        ok and blob.count("del_list") == 3 and blob.count("add_list") == 2, blob[:300])

    m = load(wd_p, "wd")
    m.router_cmd = lambda cmd: (hist2 if "show" in cmd else (None if "restart" in cmd else ""))
    ok, _o = capture(m.router_apply, ["d1.xboxlive.cn"], "1.1.1.1")
    chk("C9", "dnsmasq restart 失败 => 返回 False(不再谎报成功)", ok is False, "ok=%s" % ok)

    m = load(wd_p, "wd")
    m.router_cmd = lambda cmd: (hist2 if "show" in cmd else None)
    ok, _o = capture(m.router_apply, ["d1.xboxlive.cn"], "1.1.1.1")
    chk("C10", "uci 提交失败 => 返回 False", ok is False, "ok=%s" % ok)

    # ---- C11..C12 目标域/解析 ----
    m = load(wd_p, "wd")
    t1 = m.targets_for("Cn1", None, {})
    t2 = m.targets_for("Cn1", "1.2.3.4", dict(m.parse_hist(
        hist4 + " '/mp.microsoft.com/1.2.3.4'")))
    chk("C11", "targets_for: 无钉住只切显式表; 有漂移域名一并切走",
        t1 == list(m.DOMAINS["Cn1"]) and "mp.microsoft.com" in t2,
        "t1=%s t2=%s" % (t1, t2))
    chk("C12", "parse_hist 能解析单行多值(OpenWrt 把 list 合并一行)",
        {k: sorted(v) for k, v in m.parse_hist(hist2).items()} == {"d1.xboxlive.cn": ["8.8.8.8", "9.9.9.9"],
                                                                   "d2.xboxlive.cn": ["9.9.9.9"]},
        str(m.parse_hist(hist2)))

    # ---- C13 落盘读取容错 ----
    tmp = tempfile.mkdtemp()
    m = load(wd_p, "wd")
    m.BASE = tmp
    open(os.path.join(tmp, "results.json"), "w", encoding="utf-8").write("{坏json")
    chk("C13", "results.json 损坏 => 热备空表 + 记录 0(不抛异常)",
        m.load_hotspares("Cn1") == [] and m.load_record("Cn1") == 0.0)
    json.dump([{"group": "Cn1", "ip": "1.1.1.1", "speed": 30.0,
                "hotspares": [{"ip": "1.1.1.1", "steady": 88.8}]}],
              open(os.path.join(tmp, "results.json"), "w", encoding="utf-8"))
    chk("C13b", "load_record 优先取 hotspares[0](冠军本人)而不是快测 speed",
        m.load_record("Cn1") == 88.8, str(m.load_record("Cn1")))

    # ---- C14..C17 speedtest 候选生成 ----
    # v2.3.26: 候选来源从"省份标签"换成 rtt_rank 的实测握手排序。桩掉 rtt_rank 是唯一诚实的桩法 ——
    # 真握手要打外网(RFC5737 会吃满 RTT_TRIES×RTT_TIMEOUT), 而这里要验的是"test_pool 拿谁去粗筛"。
    def st_env(rows, regions=("广东", "广西"), screen=None, rtt=None):
        x = load(xs_p, "xst")
        x.BASE = tempfile.mkdtemp()
        x.SCREEN_REGIONS = regions
        p = os.path.join(x.BASE, "pool.txt")
        with open(p, "w", encoding="utf-8") as f:
            for ip, tag in rows:
                f.write("%s\t%s\n" % (ip, tag))
        seen = {"rows": None}

        def _sc(rs, host, path, budget=None, ms_of=None, capped_out=None, **k):
            # v2.3.31: 生产调用点现在传 ms_of/capped_out。桩不接就是 TypeError —— 而它会
            # 被 C0 兜住并报"段可执行"失败, 看起来像审计器坏了, 实际是桩过期了。
            # v2.3.32: 同理再加 **k(skip_out/zero_out/wave_out) —— 签名漂一次就要补一次,
            # 这就是把"桩必须跟得上签名"写进注释的原因。
            seen["rows"] = [ip for ip, _ in rs]
            seen["ms_of"] = ms_of
            if capped_out is not None:
                capped_out.extend(seen.get("capped") or [])
            return screen(rs) if screen else [(100.0 - i, ip) for i, (ip, _) in enumerate(rs)]
        x._screen = _sc
        # 默认: 全池都"握手很快"(只验候选拼装, 不验排序)。给 rtt= 就按它给的 IP 子集入选。
        def _rr(rs, **kw):
            keep = rs if rtt is None else [(ip, tag) for ip, tag in rs if ip in rtt]
            return [(float(i), ip, tag) for i, (ip, tag) in enumerate(keep)]
        x.rtt_rank = _rr
        x.median_quick = lambda ip, host, path: 50.0
        x.median_steady = lambda ip, host, path: 60.0
        return x, p, seen

    rows = [("1.1.%d.1" % i, "(北京 联通)") for i in range(30)]
    rows += [("2.2.%d.1" % i, "(广东 电信)") for i in range(5)]
    rows += [("3.3.%d.1" % i, "(广西 移动)") for i in range(3)]
    rows += [("4.4.1.1", "(auto1106 assets1.xboxlive.cn)")]
    # 只让"本地 5 + 本地 3 + 新增 1"这 9 个握手最快 —— 与 v2.3.25 "区域优先"的入选数刻意相同,
    # 这样失败信息里能直接看出"该测 9 个却测了 39 个"。
    fast = {ip for ip, _ in rows if not ip.startswith("1.1.")}
    x, p, seen = st_env(rows, rtt=fast)
    r, out = capture(x.test_pool, "Cn1", p, "h", "/u", "9.9.9.9")
    chk("C14", "粗筛候选来自 RTT 预筛(L0 给几个就测几个, 不再由省份标签决定谁能进)",
        len(seen["rows"]) == 9 and "RTT 预筛" in out,
        "screened=%d %s" % (len(seen["rows"]), out[:120]))

    x, p, seen = st_env(rows, rtt=set())
    r, out = capture(x.test_pool, "Cn1", p, "h", "/u", None)
    chk("C15", "RTT 预筛一个可达节点都没有 => 退回全池粗筛(不能返回空 = 一轮抖动把整组判无冠军)",
        len(seen["rows"]) == len(rows) and "退回全池粗筛" in out,
        "screened=%d out=%r" % (len(seen["rows"]), out[:120]))

    many = [("1.%d.%d.1" % (i // 250, i % 250), "(广东 电信)") for i in range(40)]
    x, p, _ = st_env(many)
    r, out = capture(x.test_pool, "Cn1", p, "h", "/u", "9.9.9.99")
    chk("C16", "候选超过上限时截断, 但当前 IP 必须留下",
        "9.9.9.99" in [ip for sp, ip in r["ranked"]] and "上限" in out,
        "n=%d" % len(r["ranked"]))

    # C17: 预算耗尽提前退出时不得 KeyError, 且用已测结果
    x, p, _ = st_env(many)
    x.QUICK_BUDGET_SEC = 0.01   # 每个候选 0.02s, 首测完预算即耗尽 -> 触发提前 break 分支
    cnt = {"n": 0}

    def mq(ip, host, path):
        cnt["n"] += 1
        import time as _t
        _t.sleep(0.02)
        return 50.0
    x.median_quick = mq
    r, out = capture(x.test_pool, "Cn1", p, "h", "/u", None)
    chk("C17", "快测预算用尽提前 break 不抛 KeyError, 用已测结果",
        r is not None and "预算" in out, out[-200:])

    # ---- C67..C67b 粗筛总墙钟预算(F3) ----
    # 单节点上限封不住**总**时长: 连接 DL_TIMEOUT 12s + 读数 QUICK_MAX_SEC 25s = 最坏 37s,
    # 4 并发 30 个节点就是 296s, 而粗筛在决策链最前面、一条决策都不产出。原实现 ex.map
    # 会把**全部**节点提交并等全部跑完才返回, 池子还会随 pool_refresh 继续长。
    rows67 = [("10.0.0.%d" % i, "(测试)") for i in range(1, 25)]
    x67 = load(xs_p, "x67")
    x67.SCREEN_WORKERS = 4

    def _slow67(ip, host, path, chunk, meta=None):
        # v2.3.31: _screen 现在把 meta 传下来(撞顶要回报)。桩不接就是 TypeError ->
        # 线程里被兜成 0.0 -> 全被 sp>0 过滤 -> 这条会以"返回=0"的样子红, 看起来像预算坏了。
        time.sleep(0.12)
        return 25.0

    x67.dl_speed = _slow67
    t67 = time.perf_counter()
    r67, out67 = capture(x67._screen, rows67, "h", "/u", 0.35)
    el67 = time.perf_counter() - t67
    chk("C67", "粗筛有总墙钟预算: 24 个节点/budget 0.35s => 到点收手并明说被截断(不是默默跑满)",
        0 < len(r67) < 24 and "粗筛预算" in out67 and el67 < 3.0,
        "返回=%d 耗时=%.2fs out=%r" % (len(r67), el67, out67.strip()[:200]))

    # 下限保护: 劣化网络下每个节点都吃满超时, 预算会秒空。收太早候选表会空 -> 整组被判"无冠军",
    # 那是把一个活着的组写成死的, 比"慢"严重。所以 SCREEN_MIN_DONE 之前不许收手。
    x67b = load(xs_p, "x67b")
    x67b.SCREEN_WORKERS = 8
    x67b.SCREEN_MIN_DONE = 8

    def _slow67b(ip, host, path, chunk, meta=None):
        time.sleep(0.05)
        return 20.0

    x67b.dl_speed = _slow67b
    r67b, out67b = capture(x67b._screen, rows67[:12], "h", "/u", 0.0)
    chk("C67b", "预算不吃掉最少完成数(预算秒空时仍等够 SCREEN_MIN_DONE 个, 否则候选表空=整组判无冠军)",
        len(r67b) >= 8, "返回=%d out=%r" % (len(r67b), out67b.strip()[:160]))

    # ---- C68..C70 L0 RTT 预筛(v2.3.26) ----
    # 这一段钉的是"候选从哪来"。三个失效形态各自都能让审计保持全绿:
    #   (a) 取最后一次而非 min  -> 一个稳态 89MiB/s 的节点被一次 1020ms 抖动判成"远"(真机 124.226.72.41)
    #   (b) 握手失败记 0ms      -> 死节点排第一, 把真节点挤出前 RTT_TOP 个(真机 Cn1 4 / Cn2 14 个死节点)
    #   (c) 排序看省份标签      -> 21 个"标签外省但 ≤20ms"的近节点永远进不了粗筛(长沙/南昌/福州)
    # 都不碰网络: create_connection / perf_counter 换成受控序列, _rtt_min 只做算术。
    import types as _types

    def _rtt_env(seq=None, fail=False):
        """sequence of per-handshake wall times (seconds). returns (mod, calls)"""
        m = load(xs_p, "x68_%s_%s" % (seq, fail))
        m.socket = _types.SimpleNamespace(
            create_connection=(lambda *a, **k: (_ for _ in ()).throw(OSError("unreachable")))
            if fail else (lambda *a, **k: _types.SimpleNamespace(close=lambda: None)))
        ticks = []
        acc = 0.0
        for d in (seq or ()):
            ticks.append(acc)
            acc += d
            ticks.append(acc)
        it = iter(ticks)
        m.time = _types.SimpleNamespace(perf_counter=lambda: next(it), sleep=time.sleep)
        return m

    # (a) 三次握手 [30ms, 12ms, 20ms] -> 必须 12ms。取最后一次=20ms, 取中位=20ms, 取均值=20.7ms
    m68 = _rtt_env(seq=(0.030, 0.012, 0.020))
    got68 = m68._rtt_min("203.0.113.7", tries=3)
    ok68a = got68 is not None and abs(got68 - 12.0) < 0.5

    # (b) 可达性: 三次都抛 -> None(不可达), 不是 0ms。记 0 会让死节点升序排第一。
    m68b = _rtt_env(seq=(0.05, 0.05, 0.05), fail=True)
    got68b = m68b._rtt_min("203.0.113.8", tries=3)
    ok68b = got68b is None

    # (c) 截断 + 升序 + 剔除: 真 rtt_rank, 桩 _rtt_min。6 个可达(20/15/30/10/25/18ms) + 1 个不可达, top=3
    m68c = load(xs_p, "x68c")
    ms_of = {"a": 20.0, "b": 15.0, "c": 30.0, "d": 10.0, "e": 25.0, "f": 18.0, "dead": None}
    m68c._rtt_min = lambda ip, port=443, tries=None, timeout=None: ms_of[ip]
    got68c = m68c._rtt_rank_real([(k, "(%s)" % k) for k in ms_of], top=3)
    ok68c = [ip for _, ip, _ in got68c] == ["d", "b", "f"]

    chk("C68", "RTT 取 min-of-N(不取最后一次/中位) + 握手失败剔除(不当 0ms) + 按 ms 升序截断 top",
        ok68a and ok68b and ok68c,
        "min=%r(期望12.0) 不可达=%r(期望None) 前3=%r(期望['d','b','f'])"
        % (got68, got68b, [ip for _, ip, _ in got68c]))

    # ---- C69 L0 预算与保底(与 _screen 同规矩) ----
    # 单次握手上限也封不住总时长: RTT_TRIES×RTT_TIMEOUT=4.5s × 261 个 / 32 并发 已是 37s 量级,
    # 池子再长就无边。所以给总预算; 但收太早排序样本太小, 所以有 RTT_MIN_DONE 保底。
    rows69 = [("198.51.100.%d" % i, "(测试)") for i in range(1, 81)]
    m69 = load(xs_p, "x69")
    m69.RTT_WORKERS = 4

    def _slow69(ip, port=443, tries=None, timeout=None):
        time.sleep(0.12)
        return 20.0
    m69._rtt_min = _slow69
    t69 = time.perf_counter()
    r69, out69 = capture(m69._rtt_rank_real, rows69, 443, 999, 0.35)
    el69 = time.perf_counter() - t69
    ok69a = 0 < len(r69) < 80 and "RTT 预筛预算" in out69 and el69 < 6.0

    m69b = load(xs_p, "x69b")
    m69b.RTT_WORKERS = 8
    m69b.RTT_MIN_DONE = 8

    def _slow69b(ip, port=443, tries=None, timeout=None):
        time.sleep(0.03)
        return 20.0
    m69b._rtt_min = _slow69b
    r69b, out69b = capture(m69b._rtt_rank_real, rows69[:20], 443, 999, 0.0)
    ok69b = len(r69b) >= 8

    chk("C69", "L0 有总墙钟预算(80 个/0.35s => 到点收手并明说余几个), 但预算不吃掉最少完成数 RTT_MIN_DONE",
        ok69a and ok69b,
        "预算轮: 返回=%d/80 耗时=%.2fs; 保底轮: 返回=%d/20(期望>=8) out=%r"
        % (len(r69), el69, len(r69b), out69.strip()[:160]))

    # ---- C70 标签漏标的近节点也能进候选 ----
    # 真机账: Cn1 15 个 / Cn2 6 个"标签写外省、实测 ≤20ms"的近节点(长沙 15.7/19.0、福州 18.9、
    # 南昌 18.3/18.7)因为标签不是广东广西, 在 v2.3.25 的区域优先里从来没进过粗筛。
    # 这里把标签写成"北京", 握手做成最快 —— 若有人把标签塞回排序/过滤, 它立刻掉出前 1。
    m70 = load(xs_p, "x70")
    m70._rtt_min = lambda ip, port=443, tries=None, timeout=None: {
        "198.51.100.11": 15.7, "198.51.100.12": 60.0, "198.51.100.13": 70.0}[ip]
    rows70 = [("198.51.100.11", "(北京 联通)"),
              ("198.51.100.12", "(广东 电信)"),
              ("198.51.100.13", "(广东 移动)")]
    got70 = m70._rtt_rank_real(rows70, top=1)
    chk("C70", "标签漏标的近节点也能进候选(排序只看实测 ms, 省份标签不参与取舍)",
        len(got70) == 1 and got70[0][1] == "198.51.100.11" and "北京" in got70[0][2],
        "got=%r" % (got70,))

    # ---- C18 热备分散/下限/排序 ----
    x = load(xs_p, "xst")
    x.median_steady = lambda ip, host, path: {"a": 90.0, "b": 80.0, "c": 30.0, "d": 4.0}[ip]
    tags = {"a": "(广东 电信)", "b": "(广东 移动)", "c": "(广东 联通)", "d": "(广东 广电)"}
    res = [(100.0, "a"), (99.0, "b"), (98.0, "c"), (97.0, "d")]
    out_t = capture(x.steady_top_diverse, res, set(), "h", "/u", tags, 2, set())[0]
    chk("C18", "热备取一家一个(分散) + <20 不入选 + 按稳态降序",
        [o["ip"] for o in out_t] == ["a", "b"] and all("_op" not in o for o in out_t),
        str(out_t))

    x = load(xs_p, "xst")
    x.median_steady = lambda ip, host, path: 4.0 if ip == "a" else 3.0
    out_t = capture(x.steady_top_diverse, [(9.0, "a"), (8.0, "b")], set(), "h", "/u", tags, 2, set())[0]
    chk("C18b", "全部不达标 => 名单留空 + 告警(不拿烂节点占位)", out_t == [], str(out_t))

    x = load(xs_p, "xst")
    x.median_steady = lambda ip, host, path: {"a": 95.0, "b": 90.0, "c": 85.0}[ip]
    tags2 = {"a": "(广东 电信)", "b": "(广东 电信)", "c": "(广东 移动)"}
    out_t = capture(x.steady_top_diverse, [(9.0, "a"), (8.0, "b"), (7.0, "c")],
                    set(), "h", "/u", tags2, 2, set())[0]
    chk("C18c", "冠军运营商被避让后再分散(不与冠军撞车)",
        [o["ip"] for o in out_t] == ["a", "c"], str([o["ip"] for o in out_t]))

    # ---- C19..C22 speedtest main 四个决策分支 ----
    hist_cn1 = ("dhcp.@dnsmasq[0].address='/assets1.xboxlive.cn/9.9.9.9' "
                "'/assets2.xboxlive.cn/9.9.9.9' '/d1.xboxlive.cn/9.9.9.9' '/d2.xboxlive.cn/9.9.9.9'")

    def st_run(ranked, quick_map, steady_map, hist=hist_cn1, argv=("--pool", "Cn1"), seed=None,
               apply_fail=False, calls=None, pre=None):
        x = load(xs_p, "xst")
        x.BASE = tempfile.mkdtemp()
        if seed is not None:
            json.dump(seed, open(os.path.join(x.BASE, "results.json"), "w", encoding="utf-8"))
        pth = os.path.join(x.BASE, "pool.txt")
        with open(pth, "w", encoding="utf-8") as f:
            f.write("9.9.9.9\t(广东 电信)\n1.1.1.1\t(广东 移动)\n2.2.2.2\t(广东 联通)\n")
        pth2 = os.path.join(x.BASE, "pool2.txt")
        with open(pth2, "w", encoding="utf-8") as f:
            f.write("8.8.8.8\t(广东 电信)\n")
        # 两组都在 POOLS 里(合并落盘按 POOLS 顺序), 但 --pool Cn1 只测一组
        x.POOLS = [("Cn1", pth, "assets1.xboxlive.cn", "/u",
                    ["assets1.xboxlive.cn", "assets2.xboxlive.cn", "d1.xboxlive.cn", "d2.xboxlive.cn"]),
                   ("Cn2", pth2, "dlassets.xboxlive.cn", "/u2",
                    ["dlassets.xboxlive.cn", "dlassets2.xboxlive.cn"])]
        x._screen = lambda rs, h, u, **k: ranked      # **k 吃掉 ms_of/capped_out(v2.3.31)
        x.median_quick = lambda ip, h, u: quick_map.get(ip, 0.0)

        def _ms(ip, h, u):
            # calls: 记调用序列 —— "同一个 IP 一轮测几次"是账, 不看序列只查语法就会漏(见 C45)
            if calls is not None:
                calls.append(ip)
            v = steady_map.get(ip, 0.0)
            if isinstance(v, list):      # 列表 = 该 IP 逐次返回不同的值(模拟抖动/第二次测不出)
                return v.pop(0) if len(v) > 1 else (v[0] if v else 0.0)
            return v

        x.median_steady = _ms
        rec = []
        x.router_cmd = lambda cmd: (rec.append(cmd) or
                                    (hist if "show" in cmd else (None if apply_fail else "")))
        if pre is not None:
            pre(x)          # 桩打歪/要监视调用的用例在这里挂钩子(必须在 main 之前)
        old = sys.argv
        sys.argv = ["xbox-speedtest.py"] + list(argv)
        try:
            _r, out = capture(x.main)
        finally:
            sys.argv = old
        res = json.load(open(os.path.join(x.BASE, "results.json"), encoding="utf-8"))
        return x, res, " ".join(rec), out

    x, res, blob, out = st_run([(90.0, "9.9.9.9")], {"9.9.9.9": 90.0}, {"9.9.9.9": 88.8})
    g = res[0]
    chk("C19", "无变化分支也记冠军稳态(hotspares[0]=冠军, 供看门狗做劣化基准)",
        g["router"].startswith("no change") and g["hotspares"][0]["ip"] == "9.9.9.9"
        and g["hotspares"][0]["steady"] == 88.8 and g["speed"] == 88.8,
        json.dumps(g, ensure_ascii=False)[:300])

    x, res, blob, out = st_run([(90.0, "1.1.1.1")], {"1.1.1.1": 90.0},
                               {"1.1.1.1": 60.0, "9.9.9.9": 88.0})
    g = res[0]
    chk("C20", "冠军稳态赢不过 当前×迟滞 => 保持不变(不写路由器)",
        g["ip"] == "9.9.9.9" and "kept" in g["router"] and "add_list" not in blob,
        "ip=%s router=%s" % (g.get("ip"), g.get("router")))

    x, res, blob, out = st_run([(90.0, "1.1.1.1")], {"1.1.1.1": 90.0},
                               {"1.1.1.1": 95.0, "9.9.9.9": 0.0})
    g = res[0]
    chk("C21", "当前 IP 测不出(死) => 无视迟滞直接切换, 且整组 4 域名一起写",
        g["ip"] == "1.1.1.1" and blob.count("add_list") == 4 and blob.count("del_list") == 4,
        "ip=%s add=%d del=%d" % (g.get("ip"), blob.count("add_list"), blob.count("del_list")))

    x, res, blob, out = st_run([(90.0, "1.1.1.1")], {"1.1.1.1": 90.0},
                               {"1.1.1.1": 0.0, "9.9.9.9": 0.0})
    g = res[0]
    chk("C21b", "当前与冠军都测不出 => 保守保持, 不瞎切",
        g["ip"] == "9.9.9.9" and "kept" in g["router"] and "add_list" not in blob
        and g.get("speed") == 0.0, g.get("router"))

    # 2026-11-07 真机事故: 当前 IP 判死 + 冠军稳态 14.1 < REPLACE_MIN(20) 也照切,
    # 把 93.5 MiB/s 的 211.97.83.41 换成 14.1 的 124.226.72.41。冠军不够格时宁可不切。
    x, res, blob, out = st_run([(90.0, "1.1.1.1")], {"1.1.1.1": 90.0},
                               {"1.1.1.1": 14.0, "9.9.9.9": 0.0})
    g = res[0]
    chk("C33", "当前 IP 已死但冠军稳态 < REPLACE_MIN => 不换更烂的",
        g["ip"] == "9.9.9.9" and "kept" in g["router"] and "add_list" not in blob
        and "< %d" % int(x.REPLACE_MIN) in g["router"], g.get("router"))

    # ---- C34 当前 IP 必须排在快测第一位(它是迟滞基准, 预算超时最先牺牲的就是队尾) ----
    # 2026-11-07 才看清的路径: cands 按粗筛分降序排, 当前 IP 通常垫底(截断时甚至被塞进 keep[-1]);
    # 快测预算一用尽, 第一个被 break 跳过的正是它 -> results(只留 sp>0)里没有它 ->
    # main 把"根本没测"读成"测了是 0" = 判死。用 QUICK_BUDGET_SEC=0 把"预算刚好用完"这一态钉住。
    x5 = load(xs_p, "xst5")
    pth5 = os.path.join(tempfile.mkdtemp(), "p5.txt")
    with open(pth5, "w", encoding="utf-8") as f:
        f.write("1.1.1.1\t(广东 电信)\n2.2.2.2\t(广东 移动)\n3.3.3.3\t(广东 联通)\n")
    x5.load_ips = lambda p: [("1.1.1.1", "(广东 电信)"), ("2.2.2.2", "(广东 移动)"),
                             ("3.3.3.3", "(广东 联通)")]
    x5._screen = lambda rs, h, u, **k: [(90.0, "1.1.1.1"), (80.0, "3.3.3.3")]  # 当前 IP 不在 Top, 粗筛分垫底
    x5.median_quick = lambda ip, h, u: 50.0
    x5.QUICK_BUDGET_SEC = 0.0
    rr5, _o5 = capture(lambda: x5.test_pool("Cn1", pth5, "h", "/u", "2.2.2.2"))
    chk("C34", "当前 IP 排在快测第一位(预算超时也不跳过迟滞基准)",
        rr5 is not None and "2.2.2.2" in rr5.get("quick", {}),
        "quick 键=%s" % sorted((rr5 or {}).get("quick", {}).keys()))
    src_x = open(xs_p, encoding="utf-8").read()
    chk("C34b", "main 的 q_cur 取自完整快测表 quick, 不是被 sp>0 过滤过的 results",
        "q_cur = quick.get(cur_ip[name]" in src_x,
        "命中=%d 次" % src_x.count("q_cur = quick.get(cur_ip[name]"))

    # ---- C35 看门狗体检必须测本组自己的文件 ----
    # 2026-11-07 才发现: steady(ip) 的 host/path 默认值是 Cn1 的, 而 main 里两处 steady(ip) 没传 ——
    # Cn2 的每小时体检一直在测 Cn1 的 295MiB 小文件, 再拿去比 Cn2 大包实测的记录。同一时刻实测:
    # 用 Cn1 文件 98~105MiB/s, 用自己的大包 21~24MiB/s, 差 5 倍 => degraded 永不触发, Cn2 挂半速没人管。
    wd_src = open(wd_p, encoding="utf-8").read()
    wd_tree = ast.parse(wd_src)
    fn_steady = [n for n in wd_tree.body
                 if isinstance(n, (ast.FunctionDef,)) and n.name == "steady"]
    chk("C35", "watchdog.steady 的 host/path 没有默认值(避免静默测到别组的文件)",
        bool(fn_steady) and all(len(a.args.defaults) == 0 for a in fn_steady),
        "默认参数个数=%s" % [len(a.args.defaults) for a in fn_steady])
    # 只认真正的调用(ast.Call), 不认文档串里提到的 `steady(ip)` —— 文本匹配会被自己的说明文字绊倒
    bare = [c.lineno for c in ast.walk(wd_tree)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "steady"
            and len(c.args) == 1 and not c.keywords]
    explicit = [c.lineno for c in ast.walk(wd_tree)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "steady"
                and {k.arg for k in c.keywords} == {"host", "path"}]
    chk("C35b", "watchdog 主流程调用 steady 时显式传本组的 host/path",
        not bare and len(explicit) >= 3,
        "裸调用行=%s 显式传参行=%s" % (bare, explicit))

    # ---- C36 同 IP 且稳态测不出时, 不许拿快测中位数顶账 ----
    # 2026-11-07 00:29 实跑: Cn2 快测中位 93.8、稳态 3 轮全 0, 而 results.json 的 speed 仍是 93.8 ——
    # 看门狗把它当劣化基准, 下一小时自己一测 21 就判 degraded(21 < 93.8*0.55), 白触发一次止血。
    x6, res6, blob6, out6 = st_run([(90.0, "9.9.9.9")], {"9.9.9.9": 93.8}, {"9.9.9.9": 0.0})
    g6 = res6[0]
    chk("C36", "同 IP 且稳态测不出 => speed 记 0 并明说 unmeasurable(不拿快测顶账)",
        g6["ip"] == "9.9.9.9" and g6.get("speed") == 0.0 and "unmeasurable" in g6["router"],
        "speed=%r router=%r" % (g6.get("speed"), g6.get("router")))

    # ---- C37 抖动必须留在日志里(否则事后只看中位数会以为决策依据很硬) ----
    x7 = load(xs_p, "xst7")
    seq = [10.0, 10.0, 100.0]
    x7.dl_steady = lambda ip, h, p, *a, **k: seq.pop(0)
    _v7, out7 = capture(lambda: x7.median_steady("1.2.3.4", "h", "/u"))
    chk("C37", "稳态三轮极差 >25% 时把离散度打进日志(中位数本身不解释抖动)",
        "稳态离散" in out7 and "10.0/10.0/100.0" in out7, out7.strip()[-160:])
    # 2026-11-07 00:53 真机日志: "极差 22.8 > 25%" —— 22.8 是绝对 MiB, 25% 是比例, 这行是假不等式
    # (真正比较的是 22.8 > 79.9×25% = 20.0)。日志里的比较必须把算出来的阈值一起打出来。
    chk("C40", "离散度日志打印算出来的阈值(极差是绝对 MiB, 不能和 25% 直接比)",
        "中位×25%=2.5" in out7 and "> 25%" not in out7, out7.strip()[-160:])

    # ---- C41 离散度 ≠ 结论由噪声支配 ----
    # 2026-11-07 01:00 真机: Cn1 冠军 49.4/47.9/102.1、阈值 96.5×1.15=110.98; Cn2 30.2/39.6/50.2、
    # 阈值 87.9×1.3=114.27。两条都只打了"本轮判定受抖动影响", 读起来像"结论是噪声掷出来的",
    # 但最好那一轮连阈值都没够到 —— 这两件事必须分开判: 看三轮区间落在阈值的哪一侧。
    x9 = load(xs_p, "xst9")
    x9._STEADY_ROUNDS_MEM.clear()
    x9._STEADY_ROUNDS_MEM["5.5.5.5"] = [49.4, 47.9, 102.1]
    n_robust = x9.decision_note("5.5.5.5", 96.5 * 1.15)
    chk("C41", "三轮最高值仍 < 阈值 => 判为稳健(不能说成受抖动影响)",
        "稳健" in n_robust and "受抖动支配" not in n_robust and "102.1" in n_robust,
        "note=%r" % n_robust)
    x9._STEADY_ROUNDS_MEM["6.6.6.6"] = [30.0, 100.0, 160.0]
    n_noise = x9.decision_note("6.6.6.6", 110.0)
    chk("C41b", "三轮区间横跨阈值 => 判为受抖动支配(换不换取决于抽到哪一轮)",
        "受抖动支配" in n_noise and "110.0" in n_noise, "note=%r" % n_noise)
    # 函数写对了但 main 里没调用, 等于没加: 用 AST 找 main 体内对 decision_note 的 Call
    tree9 = ast.parse(src_x)
    main_fn = [n for n in tree9.body if isinstance(n, ast.FunctionDef) and n.name == "main"]
    called9 = [n for m in main_fn for n in ast.walk(m)
               if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "decision_note"]
    chk("C41c", "main 的换/不换判定处确实调用了 decision_note(写了不调用等于没加)",
        len(called9) >= 1, "call 数=%d" % len(called9))

    # ---- C42 median_of 丢零值: 0/0/100 的"中位"是 100.0(单轮满分), 日志必须报几轮为 0 ----
    # 0 表示"这一轮没测出", 不是"0 MiB/s 的吞吐", 丢掉它是 quick 抗单包丢失的口径 —— 但稳态也继承了
    # 同一套逻辑: 三轮里两轮测不出, 记的是最好那一轮。Cn2 一晚能测到 0 / 14.1 / ... / 95.x,
    # 这条日志就是让"结论基於几轮"可见, 而不是看起来像三轮都成。
    x10 = load(xs_p, "xst10")
    seq10 = [0.0, 0.0, 100.0]
    x10.dl_steady = lambda ip, h, p, *a, **k: seq10.pop(0)
    _v10, out10 = capture(lambda: x10.median_steady("7.7.7.7", "h", "/u"))
    chk("C42", "0/0/100 中位记 100.0, 但日志必须写明 2/3 轮为 0(否则看起来像三轮都成)",
        _v10 == 100.0 and "2/3 轮为 0" in out10 and "中位只取非零轮 = 100.0" in out10,
        "median=%.1f out=%r" % (_v10, out10.strip()[-200:]))
    x10._STEADY_ROUNDS_MEM["8.8.8.8"] = [0.0, 0.0, 100.0]
    n10 = x10.decision_note("8.8.8.8", 110.0)
    chk("C42b", "判定措辞标出出数轮数(1/3 轮出数不能听起来像三轮都成)",
        "仅 1/3 轮出数" in n10 and "稳健" in n10, "note=%r" % n10)

    # ---- C63 median_of 的"全轮测不出"落点必须真的是 0.0, 不能是 None 或抛异常 ----
    # v2.3.19: 这条补的是棘轮本身。`return statistics.median(good) if good else 0.0` 在
    # is_fail_return 加 IfExp 之前**根本没被数成出口**(median_of 计数 0), 于是
    # 「全轮测不出时返回什么」这件事在棘轮里完全隐形。0.0 是调用方判"本轮无结论"的唯一凭据
    # (main 里 `if not sp` 分支), 改成 None 会让 `sp < rec*ratio` 抛 TypeError 而不是走进空值分支。
    chk("C63", "median_of 空输入/全零输入都返回 0.0(调用方靠它判'本轮无结论'), 有正值时取非零中位",
        x10.median_of([]) == 0.0 and x10.median_of([0.0, 0.0]) == 0.0
        and x10.median_of([0.0, 50.0, 0.0]) == 50.0,
        "empty=%r allzero=%r mixed=%r" % (x10.median_of([]), x10.median_of([0.0, 0.0]),
                                          x10.median_of([0.0, 50.0, 0.0])))

    # ---- C43 "稳态测不出"有两条路, 必须同一个口径: speed 记 0.0, 不许拿快测顶账 ----
    # 路径 a) 冠军就是当前 IP -> `no change (same ip, steady unmeasurable)`(v2.3.8 已修)
    # 路径 b) 冠军不是当前 IP, 当前 IP 稳态 0 但快测有数 -> `kept (cur steady unmeasurable, quick .. alive)`
    #        v2.3.12 才发现这条一直写的是 round(q_cur, 1) = **快测值**。而 pinned IP 不在 hotspares 里,
    #        load_record 只能回退到 speed -> 拿 93.8 的快测当基准, 下一小时诚实的 21~40 全判 degraded
    #        (21 < 93.8*0.55=51.6), 白触发止血+定向重测。同一个条件两处两种记账, 是修一个留一个的典型。
    _xa, rsa, _ba, _oa = st_run([(90.0, "9.9.9.9")], {"9.9.9.9": 93.8}, {"9.9.9.9": 0.0})
    _xb, rsb, _bb, _ob = st_run([(95.0, "1.1.1.1"), (90.0, "9.9.9.9")],
                                {"1.1.1.1": 95.0, "9.9.9.9": 93.8},
                                {"1.1.1.1": 0.0, "9.9.9.9": 0.0})
    chk("C43", "稳态测不出时两条路写同一个 speed(0.0): 快测值再高也不算基准",
        rsa[0].get("speed") == 0.0 and rsb[0].get("speed") == 0.0
        and "unmeasurable" in rsa[0]["router"] and "unmeasurable" in rsb[0]["router"],
        "same-ip speed=%r | kept speed=%r router=%r"
        % (rsa[0].get("speed"), rsb[0].get("speed"), rsb[0].get("router")))
    chk("C43b", "快测值仍留在文案里(alive 那句要能看出链路活着, 只是不当基准)",
        "93.8" in rsb[0]["router"] and "alive" in rsb[0]["router"], rsb[0].get("router"))

    # ---- C44 20MiB/s 这个门槛现在有三份拷贝, 一份都不许单独改 ----
    # REPLACE_MIN(判死时替换者下限) / steady_top_diverse(min_steady=) / watchdog.HOTSPARE_MIN_MIBS(止血线)。
    # 三处语义相同: "低于它就不值得换过去"。代码注释写了"两处门槛必须一致"却没人检查,
    # 而它们的失效方式就是 2026-11-07 那次事故: 门槛一松, 14.1 的冠军也能把 93.5 的节点顶掉。
    _xq = load(xs_p, "xst44")
    _wq = load(wd_p, "wd44")
    _mn = inspect.signature(_xq.steady_top_diverse).parameters["min_steady"].default
    chk("C44", "三份 20MiB/s 门槛(REPLACE_MIN / min_steady / HOTSPARE_MIN_MIBS)必须相等",
        _xq.REPLACE_MIN == _wq.HOTSPARE_MIN_MIBS == _mn,
        "REPLACE_MIN=%r min_steady=%r HOTSPARE_MIN_MIBS=%r"
        % (_xq.REPLACE_MIN, _mn, _wq.HOTSPARE_MIN_MIBS))

    # ---- C45 每个 IP 一轮只许稳态测一次: 六个分支都要, 不只是"保持"那一条 ----
    # 2026-11-07 第七次审计: v2.3.9 修"冠军被测两次"时只在保持分支手写了 known={cand, cur},
    # 另两个分支各漏一个 key —— 切换分支漏当前 IP、st_cur==0 分支漏冠军。C38b 只检查
    # "每个调用点都传了 known=" 这个**语法事实**, 所以上一轮全套断言与变异全过, 这个洞还在。
    # 代价: 该 IP 白测 3 轮 = 0.75GiB; 更糟的是进名单的是**第二次**那个抖动值, Cn2 单次差 4 倍,
    # 第二次返回 0 时刚测到的冠军(或刚换下来的旧节点)从热备名单里静默消失, 看门狗失去止血备胎。
    c45 = []
    _x45, r45, _b45, _o45 = st_run([(95.0, "1.1.1.1"), (90.0, "9.9.9.9")],
                                   {"1.1.1.1": 95.0, "9.9.9.9": 93.8},
                                   {"1.1.1.1": [30.0, 5.0], "9.9.9.9": 0.0}, calls=c45)
    g45 = r45[0]
    hp45 = [h for h in g45.get("hotspares", []) if h["ip"] == "1.1.1.1"]
    chk("C45", "st_cur==0 分支: 冠军只测一次, 名单用第一次的值(不是第二次的抖动值)",
        c45.count("1.1.1.1") == 1 and hp45 and hp45[0]["steady"] == 30.0,
        "router=%r 冠军被测 %d 次 hotspares=%s calls=%s"
        % (g45["router"], c45.count("1.1.1.1"), g45.get("hotspares"), c45))
    c45b = []
    _x45b, r45b, _b45b, _o45b = st_run([(95.0, "1.1.1.1"), (90.0, "9.9.9.9")],
                                       {"1.1.1.1": 95.0, "9.9.9.9": 80.0},
                                       {"1.1.1.1": 150.0, "9.9.9.9": [90.0, 5.0]}, calls=c45b)
    g45b = r45b[0]
    hp45b = [h for h in g45b.get("hotspares", []) if h["ip"] == "9.9.9.9"]
    chk("C45b", "切换分支: 换下来的旧节点也只测一次, 名单里留第一次的稳态(它是唯一备胎)",
        c45b.count("9.9.9.9") == 1 and hp45b and hp45b[0]["steady"] == 90.0,
        "router=%r 旧节点被测 %d 次 hotspares=%s calls=%s"
        % (g45b["router"], c45b.count("9.9.9.9"), g45b.get("hotspares"), c45b))

    # ---- C47 pool_refresh 必须走 DoH(443): 53 端口在全网段被钉 IP 名单劫持 ----
    # 2026-11-07 查实: 这 6 个域名全在 dnsmasq 的 address=/<域名>/<ip> 名单里, NAS 上对**任何**
    # 53 解析器(默认 / 223.5.5.5 / 114.114.114.114 / 8.8.8.8 / 119.29.29.29)都返回**当前钉住的那个
    # IP 本身**, 于是 getaddrinfo 只可能拿到已在池里的地址 -> appended 永远 0 -> 池子自 10-13 冻结 25 天,
    # 而日志一直说"appended 0 属正常"。这个洞不是"可能出错", 是**必然为 0 且看不出来**。
    pr_src = open(os.path.join(d, "pool_refresh.py"), encoding="utf-8").read()
    pr = load(os.path.join(d, "pool_refresh.py"), "pr47m")
    gai = [n.lineno for n in ast.walk(ast.parse(pr_src))
           if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "getaddrinfo"]
    chk("C47", "pool_refresh 用 DoH(443) 解析, 不用 socket.getaddrinfo(53 被钉 IP 名单劫持, 永远 appended 0)",
        not gai and getattr(pr, "DOH_ENDPOINT", "").startswith("https://")
        and callable(getattr(pr, "doh_a", None)),
        "getaddrinfo 调用行=%s DOH_ENDPOINT=%r" % (gai, getattr(pr, "DOH_ENDPOINT", None)))

    # 日志口径: appended 0 单独出现时, 分不清"池子本来最新"和"一个都没解析到"(后者冻结了 25 天没人发现)
    chk("C47b", "pool_refresh 日志同时打 resolved 与 appended(否则 appended 0 无法归因)",
        re.search(r'"resolved",\s*resolved', pr_src) is not None,
        "命中 resolved 打印=%d" % len(re.findall(r'"resolved"', pr_src)))

    # 空 A 回答(dlassets2 会间歇性只给 CNAME)与查询失败必须区分: 前者这次跳过, 后者说明 DoH 出口坏了
    pr2 = load(os.path.join(d, "pool_refresh.py"), "pr47")
    base47 = tempfile.mkdtemp()
    with open(os.path.join(base47, "IP.XboxCn1.txt"), "w", encoding="utf-8") as f:
        f.write("1.1.1.1\t(既有 广东 电信)\n")

    def _doh47(name):
        if name == "empty.test":
            return []
        if name == "boom.test":
            raise OSError("doh down")
        return ["9.9.9.9"]

    pr2.doh_a = _doh47
    _a47, out47 = capture(pr2.refresh, {"IP.XboxCn1.txt": ["empty.test", "boom.test", "ok.test"]},
                          "auto1107", base47)
    pool47 = open(os.path.join(base47, "IP.XboxCn1.txt"), encoding="utf-8").read()
    chk("C47c", "空 A 回答(dlassets2 会间歇性只给 CNAME)与 DoH 失败分开处理: 前者跳过且不打 ERR, 后者只跳过该域名",
        "无 A 记录" in out47 and "boom.test ERR" in out47 and "resolved 1" in out47
        and "9.9.9.9" in pool47 and "1.1.1.1" in pool47,
        out47.strip()[:300])

    # ---- C66 pool_refresh 写入前必须校验 A 记录(F2) ----
    # DoH 的 Answer[].data 是**上游给的字符串**。此前唯一的门是 existing 去重, 也就是"不重复就写"。
    # 写进池子之后下游三处全拿它当节点: load_ips 的正则认点分、粗筛白等一个超时、稳态 0 被读成
    # "节点已死"; 最坏被 router_apply 钉进 dnsmasq —— 那一步的后果不是"慢", 是整组断网。
    pr66 = load(os.path.join(d, "pool_refresh.py"), "pr66")
    base66 = tempfile.mkdtemp()
    with open(os.path.join(base66, "IP.XboxCn1.txt"), "w", encoding="utf-8") as f:
        f.write("1.1.1.1\t(既有 广东 电信)\n")
    junk66 = ["0.0.0.0", "127.0.0.1", "192.168.1.1", "10.1.2.3", "100.64.0.9",
              "169.254.169.254", "255.255.255.255", "224.0.0.1", "not-an-ip", "", "::1"]
    pr66.doh_a = lambda name: junk66 if name == "junk.test" else ["9.9.9.9"]
    _a66, out66 = capture(pr66.refresh, {"IP.XboxCn1.txt": ["junk.test", "ok.test"]}, "auto1107", base66)
    pool66 = open(os.path.join(base66, "IP.XboxCn1.txt"), encoding="utf-8").read()
    leaked66 = [x for x in junk66 if x and x in pool66]
    chk("C66", "pool_refresh 写入前校验: 非公网/非法 A 记录(0.0.0.0/环回/私网/CGNAT/组播/IPv6)一律不写进池子",
        not leaked66 and "9.9.9.9" in pool66 and "reject" in out66
        and pr66.ip_reject_reason("9.9.9.9") is None,
        "漏写=%s 池=%r out=%r" % (leaked66, pool66[:160], out66[:300]))

    # ---- C48 定向重测冷却: 冷却期内只止血不重测 ----
    # 2026-10-27 真机: 20/21/22/23 连续四小时各触发一次全量(3405s/2069s/1284s/1045s)。
    # 单组 3.71GiB -> 组整夜死着时 24 次/天 ≈ 89GiB, 且大概率测完还是老样子。
    def wd_run(mark_age):
        """mark_age=None 表示没有标记文件; 负数表示标记时间在未来"""
        m = load(wd_p, "wd48")
        m.BASE = tempfile.mkdtemp()
        if mark_age is not None:
            with open(os.path.join(m.BASE, m.RETEST_MARK_NAME), "w", encoding="utf-8") as f:
                f.write("%d\n" % int(time.time() - mark_age))
        m.get_pins = lambda: {"Cn1": "9.9.9.9", "Cn2": "8.8.8.8"}
        m.steady = lambda ip, host=None, path=None: 0.0 if ip == "9.9.9.9" else 100.0
        m.load_hotspares = lambda n: [{"ip": "7.7.7.7", "steady": 99}] if n == "Cn1" else []
        m.router_cmd = lambda cmd: (hist4 if "show" in cmd else "")
        ran = []
        m.subprocess = type("S", (), {"run": staticmethod(lambda *a, **k: ran.append(a))})()
        _r, o = capture(m.main)
        return m, o, ran

    w48c = load(wd_p, "wd48c")
    _m48, out48, ran48 = wd_run(60)
    chk("C48", "冷却期内(1 分钟前刚测过): 不触发重测, 但止血照做(止血是分钟级、几 MB 的事)",
        not ran48 and "止血成功" in out48 and "cooldown" in out48, out48[-300:])
    _m48b, out48b, ran48b = wd_run(w48c.RETEST_COOLDOWN + 60)
    chk("C48b", "超过冷却: 照常触发定向重测",
        bool(ran48b) and "--pool Cn1" in out48b, out48b[-300:])
    _m48c, out48c, ran48c = wd_run(None)
    chk("C48c", "无标记(首次部署/标记被删): 不冻结重测, 照常触发",
        bool(ran48c), out48c[-300:])
    _m48d, out48d, ran48d = wd_run(-3600)
    chk("C48d", "标记时间在未来(时钟回拨/手工写错): 按没见过处理, 一次误写不得把重测冻死",
        bool(ran48d), out48d[-300:])

    # ---- C49 冷却标记单写者: 主程序写, 看门狗只读 ----
    # 两边各写一份是这项目吃过亏的老路(三份 20MiB/s 门槛各写各的, 靠 C44 才钉住)
    wd_src = open(wd_p, encoding="utf-8").read()
    xs_src = open(xs_p, encoding="utf-8").read()
    # 按"同一行里既出现标记名又出现写模式"判写者 —— 别看 open( 的参数列表: 标记名埋在
    # os.path.join(BASE, ...) 里面, 正则一遇到内层右括号就截断(C49 首版就是这么写歪的)
    wd_writes = [l.strip() for l in wd_src.splitlines() if "RETEST_MARK_NAME" in l and '"w"' in l]
    xs_writes = [l.strip() for l in xs_src.splitlines() if "RETEST_MARK_NAME" in l and '"w"' in l]
    name49 = load(xs_p, "xst49n").RETEST_MARK_NAME
    chk("C49", "冷却标记单写者(speedtest 写, watchdog 只读)且两边文件名一致",
        name49 == w48c.RETEST_MARK_NAME and len(xs_writes) == 1 and not wd_writes,
        "name=%r/%r speedtest写=%d watchdog写=%s"
        % (name49, w48c.RETEST_MARK_NAME, len(xs_writes), wd_writes))

    x49, _r49, _b49, _o49 = st_run([(90.0, "9.9.9.9")], {"9.9.9.9": 90.0}, {"9.9.9.9": 88.0})
    mp49 = os.path.join(x49.BASE, x49.RETEST_MARK_NAME)
    # v2.3.34 标记改成按组的 JSON {"Cn1": epoch}。这里必须跟着改: 直接 int(文本) 会在
    # C0「段可执行」里炸成 ValueError, 而 C0 只会说"崩了", 不会说"读标记的口径过时了"。
    def _age49(path):
        raw = open(path, encoding="utf-8").read().strip()
        obj = json.loads(raw)
        if isinstance(obj, dict):
            obj = max(obj.values()) if obj else 0
        return time.time() - float(obj)
    age49 = _age49(mp49) if os.path.exists(mp49) else 1e9
    chk("C49b", "主程序每轮开头都打标记(全量 cron / 手动 / 看门狗触发都算一次重测)",
        0 <= age49 < 300, "标记 age=%.0fs 存在=%s" % (age49, os.path.exists(mp49)))

    x49c = load(xs_p, "xst49c")
    x49c.BASE = os.path.join(tempfile.mkdtemp(), "不存在", "不可写")
    try:
        capture(x49c.mark_retest)
        err49 = ""
    except Exception as e:
        err49 = "%s: %s" % (type(e).__name__, e)
    chk("C49c", "标记写不进去(只读盘/权限/目录不存在)不抛异常 —— 记账失败不得拖垮测速本身",
        not err49, err49)

    # ---- C50 冷却必须跨得过触发周期 ----
    # v2.3.14 把冷却设成 45 分钟, 而看门狗 cron 是每小时一次: 两次触发相隔 3600s > 2700s,
    # 冷却**一次都不会命中**。C48 桩测用"1 分钟前刚测过"当然命中, 于是全绿 —— 断言问的是
    # "冷却机制对不对", 没人问过"这个值够不够长"。真机 2026-10-27 的
    # 20:30:26 / 21:30:35 / 22:30:32 / 23:30:16(间隔 3609/3597/3584s) 因此会逐字重演。
    rd50 = open(os.path.join(d, readme_name(d)), encoding="utf-8").read()
    cron50 = re.search(r"^\s*([0-9*/,-]+)\s+([0-9*/,-]+)\s+\*\s+\*\s+\*\s+.*watchdog\.py", rd50, re.M)
    period50 = 3600 if (cron50 and cron50.group(2) == "*") else None
    chk("C50", "重测冷却跨得过看门狗 cron 周期(45 分钟 < 每小时 => 冷却一次都不命中, 2026-10-27 会重演)",
        period50 is not None and w48c.RETEST_COOLDOWN > 2 * period50,
        "cron=%r 周期=%r 冷却=%ss"
        % (cron50.group(0).strip() if cron50 else None, period50, w48c.RETEST_COOLDOWN))

    # ---- C51 收尾再打标记: 冷却按"测完"算 ----
    # 只打在开头时, 一次 3405s 的定向重测结束那一刻标记已过期 3405s —— 比整个窗口还长。
    # 用 spy 记"每次打标记时 results.json 在不在": 开头那次不在, 收尾那次必须在。
    seen51 = []

    def _pre51(m):
        orig51 = m.mark_retest

        def _spy51(*a, **k):
            seen51.append(os.path.exists(os.path.join(m.BASE, "results.json")))
            orig51(*a, **k)      # v2.3.34: mark_retest 带上组参数了, 桩必须跟着接(*a, **k)
        m.mark_retest = _spy51

    st_run([(90.0, "9.9.9.9")], {"9.9.9.9": 90.0}, {"9.9.9.9": 88.8}, pre=_pre51)
    chk("C51", "正常结束再打一次标记(冷却从测完算; 只打开头时 3405s 的长跑会吃掉整个窗口)",
        seen51 == [False, True], "打标记次数=%d 落盘前=%s" % (len(seen51), seen51))

    # ---- C52 同 IP 分支也要把决策落到路由器(否则"切了一半"永远不修) ----
    # 另外四个分支都调 router_apply, 只有"冠军==当前IP"这条直接 continue:
    # assets1 已切到 9.9.9.9、assets2 还钉着 5.5.5.5 时, 每轮只打一行 [warn], 死 IP 一直留在组里。
    hist52 = ("dhcp.@dnsmasq[0].address='/assets1.xboxlive.cn/9.9.9.9' "
              "'/assets2.xboxlive.cn/5.5.5.5' '/d1.xboxlive.cn/9.9.9.9' '/d2.xboxlive.cn/9.9.9.9'")
    x52, res52, blob52, out52 = st_run([(90.0, "9.9.9.9")], {"9.9.9.9": 90.0},
                                       {"9.9.9.9": 88.8}, hist=hist52)
    chk("C52", "同 IP 分支也调 router_apply: 组内残留(切了一半)顺手修掉, 而不是只打 [warn]",
        "add_list dhcp.@dnsmasq[0].address='/assets2.xboxlive.cn/9.9.9.9'" in blob52
        and "5.5.5.5" in blob52, "uci=%r" % blob52[:200])

    # ---- C53 看门狗与测速主程序共用同一把运行锁 ----
    # 2026-11-07 09:30 真机现场: 09:28 的手动全量跑到 09:30:56, 09:30 的看门狗在中间按
    # degraded 把 Cn2 切到 14.29.101.160; 全量 09:30:56 却按**开始时**读到的 uci 记
    # "kept 58.251.60.41". 两个进程各拿一把锁(.watchdog.lock / .run.lock) 等于没锁。
    try:
        import fcntl as _fc53
    except ImportError:
        _fc53 = None
    if _fc53 is None:
        # Windows 上锁是进程级的另一套, 拿不到可断言的现场 -> 退化为"两边用的是同一个文件名"
        chk("C53", "看门狗与测速主程序共用同一把运行锁(Windows 无 fcntl, 退化为源码同名比较)",
            '".run.lock"' in wd_src and '".run.lock"' in xs_src,
            "watchdog=%s speedtest=%s" % ('".run.lock"' in wd_src, '".run.lock"' in xs_src))
    else:
        w53 = load(wd_p, "wd53")
        w53.BASE = tempfile.mkdtemp()
        hold53 = open(os.path.join(w53.BASE, ".run.lock"), "w")
        _fc53.flock(hold53, _fc53.LOCK_EX | _fc53.LOCK_NB)
        busy53 = w53.run_lock_taken()          # 同一个文件的另一个 fd: 必须返回 False
        w53.get_pins = lambda: {"Cn1": "9.9.9.9", "Cn2": "8.8.8.8"}
        w53.steady = lambda ip, host=None, path=None: 0.0     # 两组都坏 = 一定会想止血
        w53.load_hotspares = lambda n: []
        w53.router_cmd = lambda cmd: hist4
        ran53 = []
        w53.subprocess = type("S", (), {"run": staticmethod(lambda *a, **k: ran53.append(a))})()
        _r53, o53 = capture(w53.main)
        hold53.close()
        chk("C53", "测速在跑时看门狗整轮跳过(共用 .run.lock): 不止血(不写路由器)、也不触发重测",
            busy53 is False and not ran53 and "skip" in o53 and "止血成功" not in o53,
            "busy=%r ran=%s out=%r" % (busy53, bool(ran53), o53[-160:]))

    # ---- C54 同 IP 分支的三种结果必须分开记账 ----
    # v2.3.15 给这条分支补了 router_apply, 但返回值只用 `if _ch:` 一种: 于是"uci 写失败"和
    # "uci 提交了但 dnsmasq restart 失败"都落在同一行 "no change (same ip)" 上 ——
    # 要修的事(残留不修)没修成, 日志说"没变化", 另外四个分支却统一记 apply FAILED。
    x54a, res54a, _b54a, out54a = st_run([(90.0, "9.9.9.9")], {"9.9.9.9": 90.0},
                                        {"9.9.9.9": 88.8}, hist=hist52)
    x54b, res54b, _b54b, out54b = st_run([(90.0, "9.9.9.9")], {"9.9.9.9": 90.0},
                                        {"9.9.9.9": 88.8}, hist=hist52, apply_fail=True)
    chk("C54", "同 IP 分支三态记账: 修好=residue fixed / 真没变化=no change / 写失败=apply FAILED",
        "residue fixed" in res54a[0]["router"]
        and "apply FAILED" in res54b[0]["router"]
        and "残留修复失败" in out54b,
        "ok=%r fail=%r" % (res54a[0]["router"], res54b[0]["router"]))

    # ---- C55 锁文件建不了 != 测速在跑 ----
    # run_lock_taken 原来把 open() 失败和 flock 失败都返回 False, main 一律打"测速主程序正在跑"。
    # 真出现写权限/磁盘问题时, 止血会永久跳过, 日志每小时指着一个不存在的并发进程。
    try:
        import fcntl as _fc55
    except ImportError:
        _fc55 = None
    if _fc55 is None:
        # 不能只查"有没有这个常量": 常量留着、open 失败那行改回 return False 照样绿
        # (M48 就是这样漏过一次) —— 要同时钉住"真的 return LOCK_ERR"
        chk("C55", "锁文件建不了与'被占用'分开(Windows 无 fcntl, 退化为源码三态比较)",
            "LOCK_ERR" in wd_src and "lock-unopenable" in wd_src and "return LOCK_ERR" in wd_src,
            "wd_src 缺 LOCK_ERR 定义/字面量/return")
    else:
        w55 = load(wd_p, "wd55")
        d55 = tempfile.mkdtemp()
        os.makedirs(os.path.join(d55, ".run.lock"))     # 同名目录 -> open(...,"w") 必然失败(root 也一样)
        w55.BASE = d55
        r55 = w55.run_lock_taken()
        w55.get_pins = lambda: {"Cn1": "9.9.9.9", "Cn2": "8.8.8.8"}
        w55.steady = lambda ip, host=None, path=None: 0.0
        w55.load_hotspares = lambda n: []
        w55.router_cmd = lambda cmd: hist4
        ran55 = []
        w55.subprocess = type("S", (), {"run": staticmethod(lambda *a, **k: ran55.append(a))})()
        _r55, o55 = capture(w55.main)
        chk("C55", "锁文件建不了时报'无法创建 .run.lock'(不是'测速在跑'), 且不动路由器不重测",
            r55 == w55.LOCK_ERR and "无法创建" in o55 and "正在跑" not in o55 and not ran55,
            "ret=%r out=%r" % (r55, o55[-200:]))

    # ---- C56 止血即记账 ----
    # 2026-11-07 09:30 真机: 09:30:26 切到 14.29.101.160, 而 results.json 里 Cn2 还是 58.251.60.41,
    # 一直错到 09:37 那次手动全量。没有那次手动, 会错到明早 04:00(最长 19.5 小时)。
    # speed 必须写这一刀的实测值, 不能写名单里的旧记录 —— 记旧值会让基准虚高, 又变回"每小时 degraded"。
    w56 = load(wd_p, "wd56")
    w56.BASE = tempfile.mkdtemp()
    seed56 = [{"group": "Cn1", "ip": "1.2.3.4", "speed": 40.0, "router": "kept",
               "hotspares": [{"ip": "1.2.3.4", "steady": 99.0}, {"ip": "7.7.7.7", "steady": 98.0}]},
              {"group": "Cn2", "ip": "5.6.7.8", "speed": 88.0, "router": "kept", "hotspares": []}]
    json.dump(seed56, open(os.path.join(w56.BASE, "results.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    ok56 = w56.record_pin("Cn1", "7.7.7.7", 100.3, old_ip="1.2.3.4")
    g56 = {g["group"]: g for g in json.load(open(os.path.join(w56.BASE, "results.json"),
                                                 encoding="utf-8"))}
    chk("C56", "止血后回写 results.json 的 ip/speed/router(写实测 100.3, 不是名单旧记录 98.0; 别组不动)",
        ok56 and g56["Cn1"]["ip"] == "7.7.7.7" and g56["Cn1"]["speed"] == 100.3
        and "watchdog 止血" in g56["Cn1"]["router"]
        and [h["ip"] for h in g56["Cn1"]["hotspares"]] == ["1.2.3.4", "7.7.7.7"]  # 成员与顺序不动
        and g56["Cn1"]["hotspares"][0]["steady"] == 99.0                          # 没换掉的那台不动
        and g56["Cn1"]["hotspares"][1]["steady"] == 100.3                         # 切到的那台 = 这一刀(C58)
        and g56["Cn2"] == seed56[1]
        and not os.path.exists(os.path.join(w56.BASE, "results.json.tmp")),
        "ok=%r Cn1=%r Cn2=%r" % (ok56, g56.get("Cn1"), g56.get("Cn2")))

    # ---- C58 基准必须跟着 IP 走(注释声称的效果, 用 load_record 的返回值验, 不查字段) ----
    # record_pin 只写 grp.speed 时, load_record 先在 hotspares 里找 pinned IP 的旧 steady —— 而止血切到的
    # IP 必然来自 hotspares, 所以那个实测值到不了基准路径。真机: 写 100.3, 下次体检基准仍读 105.4。
    chk("C58", "记录完的基准 = 这一刀实测值(load_record 真返回 100.3, 不是名单旧值 98.0)",
        w56.load_record("Cn1", "7.7.7.7") == 100.3,
        "load_record=%r hotspares=%r" % (w56.load_record("Cn1", "7.7.7.7"),
                                         g56["Cn1"]["hotspares"]))

    w56b = load(wd_p, "wd56b")
    w56b.BASE = tempfile.mkdtemp()
    no56 = w56b.record_pin("Cn1", "7.7.7.7", 100.0)
    chk("C56b", "results.json 缺失/没有该组时 record_pin 返回 False 且不造文件(记账失败不拖垮止血)",
        no56 is False and not os.path.exists(os.path.join(w56b.BASE, "results.json")),
        "ret=%r" % (no56,))

    w56c, _r56c = wd_fresh(hist4, [{"ip": "7.7.7.7", "steady": 99}], {"7.7.7.7": 99.0})
    json.dump([{"group": "Cn1", "ip": "1.2.3.4", "speed": 40.0, "router": "kept",
                "hotspares": [{"ip": "7.7.7.7", "steady": 99}]}],
              open(os.path.join(w56c.BASE, "results.json"), "w", encoding="utf-8"))
    ip56c = capture(w56c.quick_fix, "Cn1", "1.2.3.4")[0]
    g56c = json.load(open(os.path.join(w56c.BASE, "results.json"), encoding="utf-8"))[0]
    chk("C56c", "quick_fix 止血成功当场回写(不是'下次再说'): ip=新 IP, speed=这一刀实测",
        ip56c == "7.7.7.7" and g56c["ip"] == "7.7.7.7" and g56c["speed"] == 99.0,
        "ip=%r rec=%r" % (ip56c, g56c))

    # ---- C57 热备跨组重复 ----
    # 2026-11-07 09:37 的 results.json: Cn2 的 hotspares[1] = 125.88.64.72 = Cn1 钉着的 IP。
    # 名单按运营商分散, 却没按"另一组钉着的 IP"去重 -> 单节点故障同时打掉两组的热备。
    w57, _r57 = wd_fresh(hist4, [{"ip": "7.7.7.7", "steady": 99}], {"7.7.7.7": 99.0})
    _ip57, out57 = capture(lambda: w57.quick_fix("Cn1", "1.2.3.4", other_pins={"7.7.7.7"}))
    chk("C57", "热备恰是另一组钉住的 IP 时告警(单节点故障会同时打掉两组)",
        "同时是另一组" in out57, out57[-200:])

    # ---- C59 文档里那句"体检统一用 Cn1 文件"是 v2.3.8 之前的实现, 已失效五版 ----
    # 它不是无害的口误: 它把爆炸半径说小了(暗示"版本化 URL 下架 = speedtest 瞎, watchdog 免疫"),
    # 实际两侧 Cn2 同时瞎 —— 止血验证用的就是同一个死 URL, 一个热备都验不出来。
    wd_src59 = open(wd_p, encoding="utf-8").read()
    rd59 = open(os.path.join(d, readme_name(d)), encoding="utf-8").read()

    def _stale59(text):
        """只抓"当成现状在写"的旧口径。历史注记里引用原话是合法的(否则改不了这段历史),
        所以带历史标记的行放过 —— 但这正是最容易被用来绕过断言的一类, 标记词得是明确的。"""
        hits = []
        for ln in text.splitlines():
            if ("健康体检统一用 Cn1" in ln or "已用 Cn1 文件规避" in ln):
                if not any(m in ln for m in ("历史上", "之前", "已失效", "过期", "原来")):
                    hits.append(ln.strip()[:70])
        return hits

    stale59 = _stale59(wd_src59) + _stale59(rd59)
    chk("C59", "体检口径文案与代码一致: 两处都写'本组自己的测速对', 不许再留'统一用 Cn1 文件'",
        not stale59 and "本组自己的测速对" in wd_src59 and "版本化 URL 是 Cn2 测量的单点" in rd59,
        "残留旧文案=%s / wd=%s / rd=%s"
        % (stale59, "本组自己的测速对" in wd_src59, "版本化 URL 是 Cn2 测量的单点" in rd59))

    # ---- C60 什么都不测的早退轮不许占满冷却 ----
    # mark_retest 原来紧跟拿锁之后, 而路由器可达性检查在十几行后: 路由器瞬断一次 -> 3 小时内不重测,
    # 而那恰恰是最该立刻重试的时刻。断言看的是"标记文件有没有被创建"(行为), 不是"哪一行在前"(语法)。
    x60 = load(xs_p, "xst60")
    x60.BASE = tempfile.mkdtemp()
    x60.router_cmd = lambda cmd: None           # ssh 不通
    old60 = sys.argv
    sys.argv = ["xbox-speedtest.py", "--pool", "Cn1"]
    try:
        _r60, out60 = capture(x60.main)
    finally:
        sys.argv = old60
    chk("C60", "路由器不可达的早退轮不写 .last_retest(否则一次瞬断白占 3 小时冷却)",
        "不可达" in out60 and not os.path.exists(os.path.join(x60.BASE, ".last_retest")),
        "out=%r mark=%r" % (out60[-120:], os.path.exists(os.path.join(x60.BASE, ".last_retest"))))

    # ---- C38 冠军的稳态一轮只许测一次(测完就复用, 别再被 steady_top_diverse 捞出来重测) ----
    # 2026-11-07 00:44 NAS 全量实跑 250s / 8.04GiB 才看出来: 保持分支里冠军先被 median_steady 测成
    # st_cand, 又被 steady_top_diverse 从快测序里捞出来重测一遍 —— 同一 IP 一轮两份 256MiB×3 = 0.75GiB
    # 白烧; 更糟的是**进热备名单的是第二次那个值**。Cn2 单次抖动能到 4 倍(v2.3.8 ⑩), 第二次返回 0
    # 时, 刚测到 95.7 的冠军直接低于 min_steady 被过滤, 名单静默少一个人还没人报警。
    x8 = load(xs_p, "xst8")
    x8.BASE = tempfile.mkdtemp()
    pth8 = os.path.join(x8.BASE, "p8.txt")
    with open(pth8, "w", encoding="utf-8") as f:
        f.write("9.9.9.9\t(广东 电信)\n1.1.1.1\t(广东 移动)\n2.2.2.2\t(广东 联通)\n3.3.3.3\t(湖北 电信)\n")
    x8.POOLS = [("Cn1", pth8, "assets1.xboxlive.cn", "/u",
                 ["assets1.xboxlive.cn", "assets2.xboxlive.cn", "d1.xboxlive.cn", "d2.xboxlive.cn"])]
    q8 = {"1.1.1.1": 90.0, "2.2.2.2": 85.0, "3.3.3.3": 80.0, "9.9.9.9": 60.0}
    x8._screen = lambda rs, h, u, **k: sorted(((v, k2) for k2, v in q8.items()), reverse=True)
    x8.median_quick = lambda ip, h, u: q8.get(ip, 0.0)
    x8.router_cmd = lambda cmd: (hist_cn1 if "show" in cmd else "")
    x8.dl_steady = lambda ip, h, p, *a, **k: 0.0
    # 每个 IP 给一串值: 冠军第一次 95(它靠这个值当上冠军), 第二次 5 —— 正好落在 min_steady(20) 之下
    seq8 = {"1.1.1.1": [95.0, 5.0], "9.9.9.9": [88.0], "2.2.2.2": [30.0], "3.3.3.3": [25.0]}
    calls8 = []

    def ms8(ip, h, u):
        calls8.append(ip)
        q = seq8.get(ip) or [0.0]
        return q.pop(0) if len(q) > 1 else q[0]

    x8.median_steady = ms8
    old8 = sys.argv
    sys.argv = ["xbox-speedtest.py", "--pool", "Cn1"]
    try:
        _r8, out8 = capture(x8.main)
    finally:
        sys.argv = old8
    g8 = json.load(open(os.path.join(x8.BASE, "results.json"), encoding="utf-8"))[0]
    hp8 = [h for h in g8.get("hotspares", []) if h["ip"] == "1.1.1.1"]
    chk("C38", "冠军稳态一轮只测一次(复用已测值, 不被 steady_top_diverse 重测覆盖)",
        "kept" in g8["router"] and calls8.count("1.1.1.1") == 1
        and hp8 and hp8[0]["steady"] == 95.0,
        "1.1.1.1 被测 %d 次, 名单里=%s, calls=%s"
        % (calls8.count("1.1.1.1"), hp8, calls8))
    # 光靠"调用点传了 known=" 不够: 必须每个热点都传(漏一个就等于没改), 用 AST 数, 别用文本匹配
    t8 = ast.parse(open(xs_p, encoding="utf-8").read())
    calls_td = [c for c in ast.walk(t8)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                and c.func.id == "steady_top_diverse"]
    with_known = [c.lineno for c in calls_td if any(k.arg == "known" for k in c.keywords)]
    chk("C38b", "steady_top_diverse 的每个调用点都传 known=(漏一个就等于没修)",
        len(calls_td) >= 7 and len(with_known) == len(calls_td),
        "调用点 %d 个, 传 known 的 %s" % (len(calls_td), with_known))

    # ---- C39 流量日志的乘法必须能自己乘平(打"候选数"是假账) ----
    # 老日志: "256MiB × 3 轮/节点 × 4 候选" = 3GiB/组, 而真机每组 4.02GiB —— 差额就是
    # steady_top_diverse 之外的 median_steady(冠军/当前 IP 各 3 轮)。必须打**实测节点数**。
    x9 = load(xs_p, "xst9")
    x9.dl_steady = lambda ip, h, p, *a, **k: 1.0
    x9._STEADY_NODES[0] = 0
    capture(x9.median_steady, "1.2.3.4", "h", "/u")
    capture(x9.median_steady, "5.6.7.8", "h", "/u")
    chk("C39", "_STEADY_NODES 按 median_steady 实际调用计数(两次调用=2)",
        x9._STEADY_NODES[0] == 2, "计数=%d" % x9._STEADY_NODES[0])
    src_x2 = open(xs_p, encoding="utf-8").read()
    chk("C39b", "流量日志打实测节点数(而不是 STEADY_CAND_MAX 候选数, 后者乘不平)",
        "实测节点 {_STEADY_NODES[0]}" in src_x2, "命中=%d" % src_x2.count("实测节点 {_STEADY_NODES[0]}"))

    # 定向重测只写本组, 不得覆盖另一组
    seed = [{"group": "Cn2", "ip": "8.8.8.8", "speed": 70.0, "hotspares": [{"ip": "8.8.8.8", "steady": 70.0}]}]
    x, res, blob, out = st_run([(90.0, "9.9.9.9")], {"9.9.9.9": 90.0}, {"9.9.9.9": 88.8}, seed=seed)
    chk("C22", "--pool 定向重测保留另一组热备(Cn2 不被抹掉)",
        len(res) == 2 and {g["group"] for g in res} == {"Cn1", "Cn2"},
        json.dumps([g["group"] for g in res]))

    # ---- C23 watchdog 死节点 => 止血 + 触发定向重测 ----
    m = load(wd_p, "wd")
    m.BASE = tempfile.mkdtemp()
    m.get_pins = lambda: {"Cn1": "9.9.9.9", "Cn2": "8.8.8.8"}
    m.steady = lambda ip, host=None, path=None: 0.0 if ip == "9.9.9.9" else 100.0
    m.load_hotspares = lambda n: [{"ip": "7.7.7.7", "steady": 99}] if n == "Cn1" else []
    m.router_cmd = lambda cmd: (hist4 if "show" in cmd else "")
    ran = []
    m.subprocess = type("S", (), {"run": staticmethod(lambda *a, **k: ran.append(a))})()
    _r, out = capture(m.main)
    chk("C23", "死节点 => 热备止血切整组, 再触发 --pool 定向重测",
        "止血成功" in out and "--pool Cn1" in out and ran, out[-400:])

    # ---- C24 pool_refresh 单域名解析失败不中断 ----
    pr = load(os.path.join(d, "pool_refresh.py"), "pr")
    chk("C24", "pool_refresh 保留单域名解析失败容错(异常被逐个捕获)",
        "except Exception" in open(os.path.join(d, "pool_refresh.py"), encoding="utf-8").read())

    # ---- C25 op_of 不得把域名/auto 标签冒充成运营商 ----
    # pool_refresh 写的标签是 "(auto1106 assets1.xboxlive.cn)", 末段是域名; 原实现直接取末段,
    # 于是同一家运营商的 4 个 auto 节点被当成 4 家, "分散"是假的(2026-11-06 真机数据审计)
    x = load(xs_p, "xst")
    cases = [("(auto1106 assets1.xboxlive.cn)", ""), ("(auto1106 d2.xboxlive.cn)", ""),
             ("(中国广东佛山 电信)", "电信"), ("(广东 联通)", "联通"), ("", ""), (None, ""),
             ("(39.136.121.143)", "")]
    bad = [(t, x.op_of(t)) for t, exp in cases if x.op_of(t) != exp]
    x.median_steady = lambda ip, h, u: {"a": 95.0, "b": 93.0, "c": 91.0, "d": 90.0}[ip]
    tags_u = {"a": "(auto1106 assets1.xboxlive.cn)", "b": "(auto1106 d1.xboxlive.cn)",
              "c": "(auto1106 d2.xboxlive.cn)", "d": "(中国广东佛山 电信)"}
    out_u = capture(x.steady_top_diverse, [(9.0, "a"), (8.0, "b"), (7.0, "c"), (6.0, "d")],
                    set(), "h", "/u", tags_u, 2, set())[0]
    # 修前: a/b/c 的末段是三个不同域名 -> 被当成三家运营商, 第一轮就把 a,b 选满, 真正的电信 d 被挤掉
    # 修后: a/b/c 归同一家(未知), 第一轮 = a + d
    chk("C25", "op_of 不把域名当运营商; 未知运营商第一轮只占一位(别家不被挤掉)",
        not bad and [o["ip"] for o in out_u] == ["a", "d"],
        "错标签=%s 名单=%s" % (bad, [o["ip"] for o in out_u]))

    # ---- C26 劣化基准必须跟着"路由器此刻钉的 IP"走 ----
    tmp2 = tempfile.mkdtemp()
    w = load(wd_p, "wd")
    w.BASE = tmp2
    json.dump([{"group": "Cn1", "ip": "9.9.9.9", "speed": 108.0,
                "hotspares": [{"ip": "9.9.9.9", "steady": 108.0}, {"ip": "7.7.7.7", "steady": 84.0}]}],
              open(os.path.join(tmp2, "results.json"), "w", encoding="utf-8"))
    # 止血已把路由器切到热备 7.7.7.7: 基准必须取 84, 而不是旧冠军的 108
    # (拿 108 当基准时 56 < 108*0.55 会误判 degraded, 每小时反复触发定向重测)
    r_pin, r_old = w.load_record("Cn1", "7.7.7.7"), w.load_record("Cn1")
    chk("C26", "load_record 优先取当前钉住 IP 的记录(止血后基准不残留旧冠军)",
        r_pin == 84.0 and r_old == 108.0 and w.classify(56.0, r_pin)[0] == "ok"
        and w.classify(56.0, r_old)[0] == "degraded",
        "pinned=%s 无参=%s" % (r_pin, r_old))

    # ---- C27 路由器写失败时不得记成"已切换"(假账会污染看门狗的基准) ----
    x, res, blob, out = st_run([(90.0, "1.1.1.1")], {"1.1.1.1": 90.0},
                               {"1.1.1.1": 95.0, "9.9.9.9": 20.0}, apply_fail=True)
    g = res[0]
    chk("C27", "uci 写失败 => 记保持原 IP + apply FAILED(不记新 IP、不打印 switched)",
        g["ip"] == "9.9.9.9" and g["speed"] == 20.0 and "apply FAILED" in g["router"]
        and "switched" not in g["router"] and g["hotspares"][0]["ip"] == "9.9.9.9",
        json.dumps(g, ensure_ascii=False)[:300])

    x, res, blob, out = st_run([(90.0, "1.1.1.1")], {"1.1.1.1": 90.0},
                               {"1.1.1.1": 95.0, "9.9.9.9": 0.0}, apply_fail=True)
    g = res[0]
    chk("C27b", "死节点场景写失败 => 同样不记新 IP(日志不留'已切换'假账)",
        g["ip"] == "9.9.9.9" and "apply FAILED" in g["router"] and "switched" not in g["router"],
        json.dumps(g, ensure_ascii=False)[:300])

    # ---- C28 稳态必须是"时间上限"而不是"一个 Range 下完就退" ----
    # 2026-11-06 真机: 160MB Range 在 100MiB/s 链路上 1.5s 读完 -> "8 秒稳态"只测了开头 1.5s
    class FakeConn:
        opens = 0
        per_round = 3           # 每轮 3×64KiB 后 EOF

        def __init__(self, host, ip, timeout):
            FakeConn.opens += 1

        def request(self, *a, **k):
            pass

        def getresponse(self):
            class R:
                status = 206

                def __init__(self):
                    self.n = 0

                def read(self, n):
                    if self.n >= FakeConn.per_round:
                        return b""
                    self.n += 1
                    return b"x" * 65536

                def close(self):
                    pass
            return R()

        def close(self):
            pass

    x = load(xs_p, "xst")
    x.BindIPConn = FakeConn
    FakeConn.opens = 0
    import time as _t
    sp = x.dl_steady("1.2.3.4", "h", "/u", 0.5, 100 * 1024 * 1024)
    opened = FakeConn.opens
    chk("C28", "读到 EOF 后继续请求续测(稳态覆盖整个时间窗, 不是只测开头一个 Range)",
        opened >= 2 and sp > 0, "opens=%d speed=%.2f" % (opened, sp))
    FakeConn.opens = 0
    x.BindIPConn = FakeConn
    x.dl_steady("1.2.3.4", "h", "/u", 5.0, 100 * 1024)   # 字节上限先到 -> 只发一次请求
    chk("C28b", "字节上限生效(防千兆链路把单次稳态放大到 800MB)",
        FakeConn.opens == 1, "opens=%d" % FakeConn.opens)

    # ---- C29 主程序写路由器失败同样不许谎报(同一条规矩以前只修了 watchdog, 主程序漏了) ----
    x = load(xs_p, "xst2")
    state = {"restart_ok": False}

    def _fake_rc(cmd):
        if "restart" in cmd:
            return "" if state["restart_ok"] else None
        return ""

    x.router_cmd = _fake_rc
    ok_f, det_f = x.router_apply(["d1.xboxlive.cn"], "1.1.1.1", "")
    state["restart_ok"] = True
    ok_t, det_t = x.router_apply(["d1.xboxlive.cn"], "1.1.1.1", "")
    chk("C29", "xbox-speedtest.router_apply: dnsmasq restart 失败 => False(主程序不得谎报已切换)",
        ok_f is False and "restart" in det_f.lower() and ok_t is True,
        "失败路径=(%s,%s) 成功路径=(%s,%s)" % (ok_f, det_f, ok_t, det_t))

    # ---- C30 稳态续测必须从已读字节继续 ----
    # v2.3.5 号称"读到 EOF 续测", 但 _req 的 Range 写死 bytes=0-167772159: 第二次请求把前 160MB 再下一遍。
    # 2026-11-06 NAS 真机两次请求 Range 完全相同, 268MB 流量只覆盖 160MiB, 后段 135MiB 从没测过。
    x3 = load(xs_p, "xst3")
    ranges = []

    class RResp:
        def __init__(self):
            self.left = 200 * 1024 * 1024    # 每次请求只给 200MB 就到 EOF
            self.status = 206
        def read(self, n):
            if self.left <= 0:
                return b""
            k = min(n, self.left)
            self.left -= k
            return b"\0" * k
        def close(self):
            pass

    class RConn:
        def __init__(self, host, ip, timeout):
            pass
        def request(self, m, p, headers=None):
            ranges.append(headers.get("Range"))
        def getresponse(self):
            return RResp()
        def close(self):
            pass

    x3.BindIPConn = RConn
    x3.dl_steady("1.2.3.4", "h", "/u", 60.0, 400 * 1024 * 1024)
    span = x3.RANGE_SPAN
    chk("C30", "稳态续测的 Range 起点接上一次已读字节(否则是把头部下一遍)",
        len(ranges) >= 2 and ranges[0] == "bytes=0-%d" % (span - 1)
        and ranges[1] == "bytes=%d-%d" % (200 * 1024 * 1024, 200 * 1024 * 1024 + span - 1),
        "Range 序列=%s" % ranges)
    # C30b 原写法是 span >= STEADY_MAX_BYTES —— 2026-11-07 真机把它推翻了:
    # dlassets(Cn2) 对 span=256MB / 512MB 一律 12s 超时(两个节点都一样), 8/64/80/160MB 才 206 正常。
    # span 超过 CDN 容忍值 => dl_steady 全返回 0 => 落进"当前IP测不出"分支 => 93.5 的节点被 14.1 换掉。
    # 所以正确约束是「span 落在 CDN 可接受窗内」, 不是「span 越大越好」。
    #
    # v2.3.32 修**理由**(审查方 P2-D): 原来这句理由写的是「实测 160MiB 通 / 256MiB 超时」, 而同一份
    # 台账 v2.3.14 还记着「512MiB 跨度也返 206(3.6s 收 266MiB)」—— 两条观测并列摆着, 断言只认一边,
    # 下一个人想调跨度会撞上一条「理由已被自己证伪」的红灯, 而红灯比没红灯更容易让人放弃。
    # 两条观测并不矛盾, 它们是**两种口径**:
    #   ① 206 = 服务端接受了 Range。512MiB 那次 3.6s 只收 266MiB, 是读侧提前结束, 不是读完;
    #   ② 「通」对 dl_steady 的含义是「12s 内读完 span」。256MiB 在 21~24MiB/s 的 Cn2 上
    #      物理上就要 11~12s, 实测正好卡在 DL_TIMEOUT 上 —— 这才是 160MiB 这个上限的来处。
    # 所以上限留 160, 但理由换成②; 再把①降级成带日期/出口的注释(它证明的是别的性质: CDN 不按
    # 跨度大小拒服务)。另加一条能兑现的硬约束: span 不得超过测速文件本身(Cn1 HEAD 实测 309714493B),
    # 超过它, "读完"就没有定义了。
    chk("C30b", "Range 跨度落在可读完的窗内(8MiB~160MiB 且 <= 测速文件字节数; "
         "上限来自「12s 内读得完」, 不是来自「返不返 206」)",
        8 * 1024 * 1024 <= span <= 160 * 1024 * 1024 and span <= 309714493,
        "span=%dMiB (上限 %dMiB; 观察: 512MiB 跨度返 206 但 3.6s 只收 266MiB = 没读完)"
        % (span // 1048576, x3.STEADY_MAX_BYTES // 1048576))

    # ---- C79 两个程序的跨度差异必须被打印, 且那行里的数不能漂 ----
    # 审查方 P2-C: `[span]` 那行把 160MiB 抄成**字面量**。A23 只比 README ↔ 代码, 不比代码 ↔ 代码,
    # 所以主程序把 RANGE_SPAN 改成 140 或 180 时, 这行照旧打 160 —— 而它存在的全部意义就是
    # 「让人知道两把尺子不一样」。抄一份的代价必须由断言兜住, 不然"观测"就是把一个假数念给人听。
    _wd_code = open(os.path.join(d, "watchdog.py"), encoding="utf-8").read()
    _m79 = re.search(r"RANGE_SPAN 是 (\d+)MiB", _wd_code)
    _want79 = x3.RANGE_SPAN // 1048576
    chk("C79", "watchdog 的 [span] 行: 打出来的 RANGE_SPAN 必须等于主程序真值(字面量会漂)",
        bool(_m79) and int(_m79.group(1)) == _want79 and "[span]" in _wd_code,
        "watchdog 写 %r, 主程序 RANGE_SPAN=%dMiB"
        % (_m79.group(1) + "MiB" if _m79 else "(没找到这行)", _want79))

    # ---- C31 服务器忽略 Range(200) 时不得把重传数据当成绩 ----
    x4 = load(xs_p, "xst4")
    calls = {"n": 0}

    class NResp:
        def __init__(self, status):
            self.status = status
            self.left = 200 * 1024 * 1024
        def read(self, n):
            if self.left <= 0:
                return b""
            k = min(n, self.left)
            self.left -= k
            return b"\0" * k
        def close(self):
            pass

    class NConn:
        def __init__(self, host, ip, timeout):
            pass
        def request(self, m, p, headers=None):
            calls["n"] += 1
        def getresponse(self):
            return NResp(200)      # 服务器无视 Range, 每次都从头给
        def close(self):
            pass

    x4.BindIPConn = NConn
    traf0 = x4._STEADY_TRAFFIC[0]
    x4.dl_steady("1.2.3.4", "h", "/u", 60.0, 400 * 1024 * 1024)
    moved = x4._STEADY_TRAFFIC[0] - traf0
    # 第二次请求已经发出去了才发现是 200(只能断开), 所以次数允许 2, 但**计入的字节**不能翻倍
    chk("C31", "服务器返 200(忽略 Range)时不再续测累加(否则重复数据当成绩)",
        calls["n"] <= 2 and moved <= 210 * 1024 * 1024,
        "请求次数=%d 计入字节=%.0fMB" % (calls["n"], moved / 1048576.0))

    # ---- C32 当前 IP 取"最近写入", 不是字典序最小 ----
    # parse_hist 曾返回 set(丢顺序), main 再 sorted()[0] 取字典序最小 -> 多值残留时拿废弃 IP 当迟滞基准
    x5 = load(xs_p, "xst5")
    h = x5.parse_hist("dhcp.@dnsmasq[0].address='/d1.xboxlive.cn/10.0.0.9' '/d1.xboxlive.cn/10.0.0.1'"
                      " '/d2.xboxlive.cn/10.0.0.7'")
    chk("C32", "当前 IP 取最近写入的那个(list 末尾), 多值残留时不按字典序瞎选",
        isinstance(h.get("d1.xboxlive.cn"), list)
        and h["d1.xboxlive.cn"] == ["10.0.0.9", "10.0.0.1"]
        and h["d1.xboxlive.cn"][-1] == "10.0.0.1",
        "parse_hist(d1)=%s" % (h.get("d1.xboxlive.cn"),))

    # ---- C32b 同一缺陷的另一份实现(watchdog 也有一份 parse_hist) ----
    # 2026-11-06 教训: 修主程序时只改了主程序那份, watchdog 的 get_pins 还在 sorted(ips)[0] 取字典序最小
    wm = load(os.path.join(d, "watchdog.py"), "wdm_c32")
    hw = wm.parse_hist("dhcp.@dnsmasq[0].address='/x1/10.0.0.9' '/x1/10.0.0.1'")
    chk("C32b", "watchdog 的 parse_hist 同样保序(同一条缺陷的两份实现必须一起修)",
        isinstance(hw.get("x1"), list) and hw["x1"][-1] == "10.0.0.1",
        "watchdog.parse_hist(x1)=%s" % (hw.get("x1"),))


    # ---- C64 get_pins 必须把"ssh 挂"与"空配置"分开 ----
    # v2.3.18 只在主程序 xbox-speedtest.py:635 加了 `|| true`, watchdog 这份没加, 于是
    # "路由器配置被 del_list 清空"与"ssh 不通"在日志里是同一句 `[skip] 路由器不可达或无钉住记录`
    # —— 该查配置的问题把人指向查网络。这条测的是**返回值语义**(None vs {}), 不是命令串(C64b 管那个)。
    wm3 = load(os.path.join(d, "watchdog.py"), "wdm_c64")
    wm3.router_cmd = lambda cmd: None
    _p_none = wm3.get_pins()
    wm3.router_cmd = lambda cmd: ""
    _p_empty = wm3.get_pins()
    wm3.router_cmd = lambda cmd: "dhcp.@dnsmasq[0].address='/assets1.xboxlive.cn/1.2.3.4'"
    _p_real = wm3.get_pins()
    chk("C64", "get_pins 三态: ssh 不可达=None / 可达但无 address 记录=每组 None 的 dict / 有记录=list 末尾",
        _p_none is None
        and isinstance(_p_empty, dict) and set(_p_empty) == {"Cn1", "Cn2"}
        and not any(_p_empty.values())
        and isinstance(_p_real, dict) and _p_real.get("Cn1") == "1.2.3.4",
        "none=%r empty=%r real=%r" % (_p_none, _p_empty, _p_real))

    # ---- C64b 两个文件里**每一处**读 uci address 的命令串都必须容忍 grep 空匹配 ----
    # 为什么不能只靠 C64/C61 这种行为断言: 桩里 router_cmd 直接被替换, 真 ssh 根本不存在,
    # 删掉 `|| true` 行为照样"绿"。必须扫源码里的调用串(C61b 只看了 speedtest 的第一个调用,
    # watchdog 的三处全在它视野外 —— 2026-11-07 外部审计指出的正是这个缝)。
    def _grep_addr_calls(path):
        out = []
        for n in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "router_cmd" and n.args
                    and isinstance(n.args[0], ast.Constant)
                    and "grep address" in str(n.args[0].value)):
                out.append(n.args[0].value)
        return out
    _calls_wd = _grep_addr_calls(os.path.join(d, "watchdog.py"))
    _calls_xs = _grep_addr_calls(os.path.join(d, "xbox-speedtest.py"))
    chk("C64b", "两文件读 uci address 的每一处 router_cmd 都带 `|| true`(watchdog 三处 + 主程序一处)",
        _calls_wd and _calls_xs
        and all("|| true" in c for c in _calls_wd + _calls_xs),
        "wd=%d 处 %r / xs=%d 处 %r" % (len(_calls_wd), _calls_wd, len(_calls_xs), _calls_xs))


    # ---- C65 劣化基准读不出来时必须出声 ----
    # `classify` 里 `rec >= DEGRADE_MIN_RECORD` 对 0 永假 —— results.json 缺失/损坏时
    # degraded 档**永久失效**且原先零日志, 运维看到的"最近很稳"是假的。这条钉"必须打一行"。
    wm4 = load(os.path.join(d, "watchdog.py"), "wdm_c65")
    wm4.BASE = tempfile.mkdtemp()          # 空目录: 里面没有 results.json
    _v65, out65 = capture(lambda: wm4.load_record("Cn1", "1.2.3.4"))
    chk("C65", "results.json 读不出 => 返回 0.0 且必须打 [record] 行(degraded 失效不能静默)",
        _v65 == 0.0 and "[record]" in out65 and "degraded" in out65,
        "rec=%r out=%r" % (_v65, out65.strip()[-160:]))


    # ---- C71 版本化 URL canary 的三个答案 ----
    # 一个只能报"坏"的函数等于没有: 三个方向必须长得不一样, 否则运维没法据它行动。
    #   206/200 = 活  -> 不说话(每小时一句"没事"会把真警报埋掉)
    #   404/403 = 下架 -> 必须点名, 且说到"同一个 URL 两边一起瞎"
    #   连不上  = ?   -> 只能说"HEAD 失败", **不许**断言 URL 已下架(可能只是本机出网断了)
    w71 = load(wd_p, "wd71")

    class _Resp71(object):
        def __init__(self, st):
            self.status = st
        def close(self):
            pass

    class _Conn71(object):
        status = 206
        methods = []
        def __init__(self, host=None, *a, **k):
            # (host, timeout) 与 (host, ip, timeout) 都要吃得下: 下面的 `BindIPConn` 是同一个桩,
            # 而 `canary` 一旦改成经 BindIPConn 走(见 M86), 只认两个参数就会 TypeError。
            self.host = host
        def request(self, method, path, headers=None):
            _Conn71.methods.append(method)
        def getresponse(self):
            if _Conn71.status < 0:
                raise OSError("connection refused")
            return _Resp71(_Conn71.status)
        def close(self):
            pass

    class _ClientMod71(object):
        HTTPConnection = _Conn71
        HTTPSConnection = _Conn71

    class _HTTPMod71(object):
        client = _ClientMod71

    w71.http = _HTTPMod71
    # v2.3.31: `BindIPConn` 也必须桩掉。它是 canary 唯一另一条出门的路 —— 变异 M86 一注入,
    # canary 就绕开 `http.client.HTTPConnection` 去握真 IP, 于是这条审计在 NAS 上打了 18 次
    # 真连接、把 subprocess 的 300s 超时跑穿(本地快只是碰巧, 那些 IP 拒得快)。
    # 审计必须离线跑: 桩要覆盖被测代码**所有**出门的路, 不能只覆盖主路径。
    w71.BindIPConn = _Conn71
    # v2.3.34: canary 不再返回 bool(P3-F), 判定唯一的口是 state。所以这里查 state + 文案,
    # 并顺带钉住"返回值为 None" —— 那个 bool 是 state 的有损副本, 留着就会有人拿它当判据。
    _s71 = {}
    _Conn71.status, _Conn71.methods = 206, []
    _st206 = {}
    ok206, out206 = capture(w71.canary, "Cn2", _st206)
    _Conn71.status = 404
    _st404 = {}
    ok404, out404 = capture(w71.canary, "Cn2", _st404)
    _Conn71.status = 403
    _st403 = {}
    ok403, out403 = capture(w71.canary, "Cn2", _st403)
    _Conn71.status = 503
    _st503 = {}
    ok503, out503 = capture(w71.canary, "Cn2", _st503)
    _Conn71.status = -1
    _stErr = {}
    okerr, outerr = capture(w71.canary, "Cn2", _stErr)
    # 四档文案必须分开。2026-11-07 实测: 改之前 403/429/503 与 404 打的是**逐字相同**的
    # 一句话, 全叫人「去换 build」。403 多半只是这台出口被限流, 换了 build 明天照样 403 ——
    # 那不是漏报, 是指错方向, 比不报更费人。
    chk("C71", "canary 五档写进 state 且文案分开: 206/200 => ok 静默 / 404 => gone 点名撤版 / "
               "403·429 => denied 只说被拒(先别换 build) / 5xx => unknown 不下结论 / "
               "连不上 => failed 只说 HEAD 失败; **且五种情况返回值都是 None**(bool 是 state 的有损副本)",
        _st206.get("Cn2") == "ok" and out206.strip() == ""
        and _st404.get("Cn2") == "gone" and "404" in out404 and "canary" in out404
        and "同一个 URL" in out404
        and _st403.get("Cn2") == "denied" and "别换" in out403 and "撤版" in out403 and "不存在" in out403
        and _st503.get("Cn2") == "unknown" and "文件不存在" in out503 and "撤版" not in out503
        and _stErr.get("Cn2") == "failed" and "HEAD 失败" in outerr and "下架" not in outerr
        and all(x is None for x in (ok206, ok404, ok403, ok503, okerr))
        and set(_Conn71.methods) == {"HEAD"},
        "state=%r/%r/%r/%r/%r 返回=%r/%r/%r/%r/%r methods=%s"
        % (_st206.get("Cn2"), _st404.get("Cn2"), _st403.get("Cn2"),
           _st503.get("Cn2"), _stErr.get("Cn2"),
           ok206, ok404, ok403, ok503, okerr, sorted(set(_Conn71.methods))))


    # ---- C72 samples.jsonl 落盘: 两轮 append, 第二轮不能抹掉第一轮 ----
    # 用真实临时文件跑, 不数文本: M82("a" 改 "w")那种缺陷不报错, 只会让昨天那行安静消失 ——
    # 而文本断言恰好在有人把 open 换个写法(变量、别名函数)时无能为力。
    x72 = load(os.path.join(d, "xbox-speedtest.py"), "xst72")
    _tmp72 = tempfile.mkdtemp(prefix="smp72_")
    # v2.3.29: 重定向 BASE, 不是重定向一个 SAMPLES 常量 —— 落盘路径必须由被测代码自己现拼。
    # 若有人把路径改回模块常量, 这条会写进真目录(而且 A26 会当场点名)。
    x72.BASE = _tmp72
    _p72 = os.path.join(_tmp72, "samples.jsonl")
    _r1 = x72.build_sample("Cn1", 6.0, [(11.0, "1.1.1.1", "广东")], [(103.6, "1.1.1.1")],
                           {"1.1.1.1": 103.6}, {"1.1.1.1": "广东"})
    _r2 = x72.build_sample("Cn2", 7.0, [(12.0, "5.5.5.5", "")], [(90.0, "5.5.5.5")],
                           {"5.5.5.5": 90.0}, {})
    _n1, _o1 = capture(x72.write_samples, [_r1],
                       [{"group": "Cn1", "ip": "1.1.1.1", "speed": 103.6, "router": "kept (steady 103.6)"}])
    _n2, _o2 = capture(x72.write_samples, [_r2],
                       [{"group": "Cn2", "ip": "5.5.5.5", "speed": 90.0, "router": "switched"}])
    try:
        _lines = [json.loads(l) for l in open(_p72, encoding="utf-8") if l.strip()]
    except Exception:
        _lines = []
    chk("C72", "samples.jsonl: 两轮 append 共两行(第二轮不覆盖), 决策字段从 summary 合并, 返回写入行数",
        _n1 == 1 and _n2 == 1 and len(_lines) == 2
        and _lines[0].get("group") == "Cn1" and _lines[0].get("ip") == "1.1.1.1"
        and str(_lines[0].get("decision", "")).startswith("kept")
        and _lines[1].get("group") == "Cn2" and _lines[1].get("speed") == 90.0
        and len(_lines[0].get("l0") or []) == 1,
        "n1=%r n2=%r 行数=%d" % (_n1, _n2, len(_lines)))
    try:
        os.remove(_p72)
    except OSError:
        pass
    os.rmdir(_tmp72) if os.path.isdir(_tmp72) else None


    # ---- C73 canary 的返回值必须真的进决策(不是"被调用过") ----
    # 审查方 P1-2: main 里原来是 `for _cn in CANARY: canary(_cn)`, bool 被扔掉。于是撤版那天
    # 照旧 classify(0.0)=dead -> 逐个试热备(每个 256MiB 全 0) -> 定向重测, 每小时约 2GiB
    # 打在一个已经不存在的 URL 上, 还把钉住的 IP 换掉。A25 只钉"被调用/走 DNS/200·206 算活",
    # 所以这条要重新钉一遍"值去哪了"。
    w73 = load(wd_p, "wd73")

    class _Resp73(object):
        def __init__(self, st):
            self.status = st
        def close(self):
            pass

    class _Conn73(object):
        status = 206
        def __init__(self, host=None, *a, **k):
            pass
        def request(self, method, path, headers=None):
            pass
        def getresponse(self):
            if _Conn73.status < 0:
                raise OSError("connection refused")
            return _Resp73(_Conn73.status)
        def close(self):
            pass

    class _Client73(object):
        HTTPConnection = _Conn73
        HTTPSConnection = _Conn73

    class _HTTP73(object):
        client = _Client73

    w73.http = _HTTP73
    w73.BindIPConn = _Conn73          # 同 C71: 桩要覆盖所有出门的路, 否则 M86 会打真网络
    _g = {}
    for _st in (206, 404, 410, 403, 401, 429, 503, 500, -1):
        _Conn73.status = _st
        _g[_st] = w73.canary_gate()
    # main 的接线: gone 分支里必须有 return(否则"闸门"只是个打印)。用 ast 找 main 里
    # `if gate == "gone"` 的 body, 断言里面有 Return —— 文本里"有 return"不算, 它可能在别的分支。
    _tree73 = ast.parse(open(wd_p, encoding="utf-8").read())
    _gone_returns = False
    for _n in ast.walk(_tree73):
        if isinstance(_n, ast.FunctionDef) and _n.name == "main":
            for _b in _n.body:
                if (isinstance(_b, ast.If) and isinstance(_b.test, ast.Compare)
                        and getattr(_b.test.comparators[0], "value", None) == "gone"
                        and any(isinstance(x, ast.Return) for x in _b.body)):
                    _gone_returns = True
    chk("C73", "canary 的档位进决策: 404/410 当轮闸门(main 里真 return), 401/403/429 只降级 0 的可信度, "
               "5xx/连不上照常体检",
        _g[206][0] == "ok" and _g[404][0] == "gone" and _g[410][0] == "gone"
        and _g[403][0] == "denied" and _g[401][0] == "denied" and _g[429][0] == "denied"
        and _g[503][0] == "unknown" and _g[500][0] == "unknown" and _g[-1][0] == "ok"
        and _g[404][1].get("Cn2") == "gone" and _gone_returns,
        "206=%r 404=%r 403=%r 503=%r err=%r main_gone_return=%r"
        % (_g[206], _g[404], _g[403], _g[503], _g[-1], _gone_returns))


    # ---- C74 Cn2 的相对劣化档必须真的能触发 ----
    # 审查方 P1-3: 25 是照 Cn1 的 100MiB/s 量级定的, 而 Cn2 的记录区间是 21~24 ->
    # `rec >= 25` 在那组恒假。实测 classify(9.0, 23.9) 返回 ok: 从 24 掉到 9(-62%)判"稳定"。
    w74 = load(wd_p, "wd74")
    _c74 = {
        "cn1_25": w74.classify(9.0, 23.9)[0],                        # 默认门槛: 仍按老口径
        "cn2_10": w74.classify(9.0, 23.9, floor=w74.DEGRADE_MIN_RECORD["Cn2"])[0],
        "cn2_ok": w74.classify(22.0, 23.9, floor=w74.DEGRADE_MIN_RECORD["Cn2"])[0],
        "unmeasured": w74.classify(0.0, 23.9, untrusted=True)[0],
        "dead": w74.classify(0.0, 23.9)[0],
    }
    _wdsrc74 = open(wd_p, encoding="utf-8").read()
    chk("C74", "劣化门槛分组: Cn2(floor=10) 从 24 掉到 9 判 degraded, 掉到 22 仍判 ok; "
               "canary 被拒时 0 分判 unmeasured 而不是 dead",
        _c74["cn1_25"] == "ok" and _c74["cn2_10"] == "degraded" and _c74["cn2_ok"] == "ok"
        and _c74["unmeasured"] == "unmeasured" and _c74["dead"] == "dead"
        and "DEGRADE_MIN_RECORD.get(name" in _wdsrc74,
        "classify 实测=%r" % (_c74,))


    # ---- C75 读侧校验: 池文件里的内网/畸形地址不许进候选 ----
    # 审查方 P3-6 实测: load_ips 收下 1.2.3.4.5 / 0.0.0.0 / 192.168.1.9。前两个只是白等超时,
    # 192.168.x 会被钉进 dnsmasq —— 那一步的后果不是"慢", 是整组断网。
    x75 = load(os.path.join(d, "xbox-speedtest.py"), "xst75")
    _t75 = tempfile.mkdtemp(prefix="pool75_")
    _p75 = os.path.join(_t75, "IP.XboxCn1.txt")
    with open(_p75, "w", encoding="utf-8") as _fh:
        _fh.write("1.2.3.4.5 (畸形)\n0.0.0.0 (本网络)\n192.168.1.9 (内网)\n"
                  "10.1.2.3 (内网)\n169.254.169.254 (元数据)\n8.8.8.8 (好)\n\n")
    _rows75, _o75 = capture(x75.load_ips, _p75)
    chk("C75", "load_ips 复用写侧网段表: 畸形/0.0.0.0/内网/链路本地一律拒收并打一行, 公网地址照收",
        [r[0] for r in _rows75] == ["8.8.8.8"] and "跳过" in _o75,
        "rows=%r out=%r" % (_rows75, _o75.strip()[:80]))
    try:
        os.remove(_p75)
    except OSError:
        pass
    if os.path.isdir(_t75):
        os.rmdir(_t75)


    # ---- C76 粗筛同分按 L0 握手排, 撞顶节点要回报 ----
    # 审查方 P1-1: 8MiB 在 80MiB/s 以上全撞 SPEED_MIN_EL 地板, 同分后第二键是 **IP 字符串**,
    # 于是"谁是冠军"由字典序决定。实测 [(80.0,'8.8.8.8'),(80.0,'116.110.9.9'),(80.0,'1.1.1.1')]。
    x76 = load(os.path.join(d, "xbox-speedtest.py"), "xst76")
    _cap76 = []

    def _dl76(ip, host, path, chunk, meta=None):
        if meta is not None:
            meta.update({"el": 0.02, "capped": True, "bytes": chunk})
        return 80.0
    x76.dl_speed = _dl76
    _ms76 = {"8.8.8.8": 31.0, "116.110.9.9": 12.0, "1.1.1.1": 9.0}
    _r76, _o76 = capture(x76._screen, [("8.8.8.8", ""), ("116.110.9.9", ""), ("1.1.1.1", "")],
                         "h", "/p", 90.0, _ms76, _cap76)
    chk("C76", "粗筛: 同分时按 L0 握手 ms 排(不再按 IP 字典序), 撞 SPEED_MIN_EL 地板的 IP 经 "
               "capped_out 交回, 且打一行『分数只是下界』",
        [ip for _sp, ip in _r76] == ["1.1.1.1", "116.110.9.9", "8.8.8.8"]
        and sorted(_cap76) == ["1.1.1.1", "116.110.9.9", "8.8.8.8"]
        and "下界" in _o76,
        "ranked=%r capped=%r out=%r" % (_r76, _cap76, _o76.strip()[:90]))

    # ---- C77 粗筛分数的"并发分摊"必须可观测(wave) ----
    # 审查方 P1-A: stub 里 12 个**速率完全一样**的节点喂真 `_screen`, 分数是 100/50/33/25×9 ——
    # 前 3 名是"谁先进场"的产物, 与节点质量零关系。真机侧同一节点: 串行 28.9 / 4 路并发每路 21.3。
    # 这一列(dl_speed 进场时同批在跑几路)是唯一能把"同节点跨轮漂移"归因到"对手变了"的凭证 ——
    # 没有它, 改了排序也证明不了改对。**本轮只加观测, 不改排序**(方向见台账)。
    # v2.3.35: 桩仍然这么写(它只是一个能同时压出 4 路的装置), 但**不再拿桩的形状当断言**。
    x77 = load(os.path.join(d, "xbox-speedtest.py"), "xst77")
    _lock77 = threading.Lock()
    _st77 = [0]

    def _dl77(ip, host, path, chunk, meta=None):
        with _lock77:
            _st77[0] += 1
            n = _st77[0]
        time.sleep(0.01)
        with _lock77:
            _st77[0] -= 1
        if meta is not None:
            meta.update({"el": 1.0, "capped": False, "bytes": chunk})
        return 100.0 / n          # 共享管道: 进场越晚分得越少(这就是被分掉的那一份)
    x77.dl_speed = _dl77
    _w77 = {}
    _r77, _o77 = capture(x77._screen, [("10.0.0.%d" % i, "") for i in range(12)],
                         "h", "/p", budget=999.0, wave_out=_w77)
    _sp77 = [sp for sp, _ip in _r77]
    _wv77 = list(_w77.values())
    # v2.3.35 (审查方 N2): 这里原来还有一条 `_bowl = max(_sp77) >= 3 * min(_sp77)`, 验的是
    # **桩自己的假设**(共享管道 -> 进场越晚分得越少)。而本轮 C83/C85 的立项理由恰恰是真机
    # 证伪了这个模型: 2026-11-08 04:00 的 Cn1 有 41/48 个分数 > 20.0、最高单路 73.2, 出口没被
    # 打满, 分数不是"抢到的样子"。同一次 run 里 C77 要求"4 路必须差 3 倍"、C85 要求"分数可以贴
    # 80 且与并发无关", 两条对同一个物理量给出相反模型。桩返回什么排序就得表现什么 = 自己给自己
    # 出题; 万一哪天真把 `_screen` 改成"进场顺序不影响带宽"(这正是真机行为), C77 会红 —— 而它红
    # 的时候是对的。所以差倍数这一条移出: C77 只留**结构不变量**, 分数与并发的关系降到真样本观测
    # (v2.3.36 更正: 原来这里写"逐 IP 的 wave 没入库", 是事实错误 —— screen_wave 一直是
    # {ip: wave} 逐 IP 落盘的。算不出相关性是因为**单行内 wave 常无方差**, 要跨行攒档位;
    # "没入库"与"要攒"方向相反, 前者会让"4 路分摊"永远不做。分档观测见 C93 与 replay 2c)。
    chk("C77", "粗筛并发分摊可观测: wave_out 每个测到的 IP 都有『进场时同批几路』, 上界=SCREEN_WORKERS, "
               "每路都在 1..上界(分数差几倍不判 —— 那是桩的假设, 不是被测代码的性质; 见 v2.3.35 N2)",
        sorted(_w77) == sorted(ip for _sp, ip in _r77)
        and max(_wv77) == x77.SCREEN_WORKERS
        and min(_wv77) >= 1
        and all(v <= x77.SCREEN_WORKERS for v in _wv77),
        "wave=%r scores=%r" % (_w77, [round(v, 1) for v in _sp77[:5]]))

    # ---- C78 粗筛三态: 没测到 / 测了 0 分 / 有分 ----
    # 审查方 P1-B: `ranked` 只收 sp>0, 于是"预算用尽被 cancel"与"测了是 0"在样本里同形(键不存在),
    # 而 quick 早就有 quick_skip、l0 有 l0_pool/probed/alive —— 同一套论证写两遍漏了粗筛这一遍。
    x78 = load(os.path.join(d, "xbox-speedtest.py"), "xst78")

    def _dl78(ip, host, path, chunk, meta=None):
        if ip.endswith(".0.9"):
            raise OSError("dead")            # 测了但 0 分
        time.sleep(0.05)
        if meta is not None:
            meta.update({"el": 1.0, "capped": False, "bytes": chunk})
        return 12.0
    x78.dl_speed = _dl78
    # 两段分开验: 预算足够时只看得到"测了 0 分"; 预算为 0 时只看得到"没测到"。
    # 混在一次里谁先谁后由线程调度决定 —— 那会让断言随机红, 而随机红的断言等于没有断言。
    _sk78a, _ze78a = [], []
    _r78a, _ = capture(x78._screen, [("10.0.0.1", ""), ("10.0.0.9", ""), ("10.0.0.2", "")],
                       "h", "/p", budget=999.0, skip_out=_sk78a, zero_out=_ze78a)
    _sk78b, _ze78b = [], []
    _r78b, _o78b = capture(x78._screen, [("10.0.0.%d" % i, "") for i in range(12)],
                           "h", "/p", budget=0.0, skip_out=_sk78b, zero_out=_ze78b)
    _sk78, _ze78 = _sk78a + _sk78b, _ze78a + _ze78b
    _smp78 = x78.build_sample("Cn1", 1.0, [], _r78a, {}, {}, screen_skip=_sk78b, screen_zero=_ze78a,
                              screen_wave={"1.1.1.1": 4})
    # replay 的 WANTED 从**真实路径**读(变异目录不含 tests/); 但如果变异体自己带了
    # d/tests/replay.py, 就用它 —— 否则"改 replay 的键"这类变异永远抓不到(等于断言不可反证)。
    _rp78 = os.path.join(d, "tests", "replay.py")
    if not os.path.isfile(_rp78):
        _rp78 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "replay.py")
    _rpl = load(_rp78, "rpl78")
    # 预算 0 + SCREEN_MIN_DONE=6 => 至少有一批被 cancel(没测到); dead 那个是"测了 0 分"
    chk("C78", "粗筛三态分得开: 没测到(skip_out)/测了 0 分(zero_out)互不重叠, 两者都进样本"
               "且与 quick_skip 对称(replay.WANTED 三键齐全)",
        _sk78a == [] and _ze78a == ["10.0.0.9"]          # 预算够 => 只看得见"测了 0 分"
        and len(_sk78b) >= 1 and _ze78b == []           # 预算 0 => 只看得见"没测到"
        and _r78a and all(ip not in _sk78 for _sp, ip in _r78a)
        and _smp78.get("screen_skip") == _sk78b and _smp78.get("screen_zero") == _ze78a
        and all(k in _smp78 for k in ("screen_skip", "screen_zero", "screen_wave"))
        and all(k in _rpl.WANTED for k in ("screen_skip", "screen_zero", "screen_wave")),
        "零分=%r 没测到=%r smp=%r" % (_ze78a, _sk78b, sorted(_smp78)))

    # ---- C80 空 ms_of 的第二键: 退回池文件顺序, 不是 IP 字典序 ----
    # 审查方 P3-E: `ms_map.get(ip, inf)` 在全空时让每个 IP 都拿 inf, 第二键失效、第三键接管 ——
    # 实测 ["1.1.1.1","5.5.5.5","9.9.9.9"] 排成原序, 与 ms 齐全时完全相反。而 L0 全不通时
    # `test_pool` 正好走这条分支(prox 为空), 也就是它**在最该稳的时候**生效。
    def _dl80(ip, host, path, chunk, meta=None):
        if meta is not None:
            meta.update({"el": 1.0, "capped": False, "bytes": chunk})
        return 10.0                     # 全同分, 只有第二键在起作用
    x80 = load(os.path.join(d, "xbox-speedtest.py"), "xst80")
    x80.dl_speed = _dl80
    _rows80 = [("9.9.9.9", ""), ("5.5.5.5", ""), ("1.1.1.1", "")]
    _r80a, _ = capture(x80._screen, _rows80, "h", "/p", budget=999.0, ms_of={})
    _r80b, _ = capture(x80._screen, _rows80, "h", "/p", budget=999.0,
                       ms_of={"9.9.9.9": 8.0, "5.5.5.5": 40.0, "1.1.1.1": 120.0})
    chk("C80", "粗筛第二键: ms_of 空时退回池文件顺序(不是 IP 字典序), ms 齐全时按握手 ms 升序",
        [ip for _sp, ip in _r80a] == ["9.9.9.9", "5.5.5.5", "1.1.1.1"]
        and [ip for _sp, ip in _r80b] == ["9.9.9.9", "5.5.5.5", "1.1.1.1"],
        "空 ms=%r 有 ms=%r" % ([ip for _sp, ip in _r80a], [ip for _sp, ip in _r80b]))

    # ---- C81 replay 的默认值必须真的回答它自己的那个问题 ----
    # 审查方 P3-F: docstring 写"默认用 1.15 对比现行门槛", 而代码是 `new_ratio = None` ——
    # 不带参数跑一遍, 翻转恒为 0(拿旧门槛比自己), 它只打一张字段完整率。
    # **提示是对的, 默认是错的**: 一个"回放器"不带参数就不回放, 等于它证明不了任何事。
    # 这里用一行**会翻转**的夹具: Cn2 wr=1.30 现行保持, 1.15 会换 —— 无参数必须打出"翻转 1 轮"。
    _tmp81 = tempfile.mkdtemp()
    _p81 = os.path.join(_tmp81, "samples.jsonl")
    with open(_p81, "w", encoding="utf-8") as _fh:
        _fh.write(json.dumps({"ts": "2026-11-08T00:00:00", "group": "Cn2", "cur": "1.1.1.1",
                              "wr": 1.30, "st_cur": 22.0, "st_cand": 26.0, "q_cur": 25.0,
                              "ip": "2.2.2.2", "speed": 26.0, "decision": "kept"},
                             ensure_ascii=False) + "\n")
    _old81 = _rpl.BASE
    _rpl.BASE = _tmp81
    _err81 = ""
    try:
        try:
            _r81, _o81 = capture(_rpl.main, [])
        except Exception as _e81:
            # 默认值退化时(比如被改回 None)最常见的结果是**在算 abs(cur - None) 时崩**。
            # 不在这里兜住的话, 异常会一路走到 C0「段可执行」—— 那条红只说"崩了", 不说
            # "回放器没在回放"。断言必须**命名**缺陷, 否则下一个人得靠猜。
            _o81, _err81 = "", "%s: %s" % (type(_e81).__name__, _e81)
    finally:
        _rpl.BASE = _old81
        shutil.rmtree(_tmp81, ignore_errors=True)
    chk("C81", "replay 无参数时按 DEFAULT_RATIO(%g) 真的回放: 夹具里 22->26 在 1.30 下保持、"
               "1.15 下换, 必须打出『翻转 1 轮』(不是『(未指定)』)"
        % getattr(_rpl, "DEFAULT_RATIO", -1),
        getattr(_rpl, "DEFAULT_RATIO", None) is not None
        and abs(_rpl.DEFAULT_RATIO - 1.15) < 1e-9
        and "翻转 1 轮" in _o81 and "未指定" not in _o81,
        "DEFAULT_RATIO=%r %s out=%r" % (getattr(_rpl, "DEFAULT_RATIO", None), _err81,
                                        _o81.strip().splitlines()[-3:]))

    # ---- C82 撞顶线是**每路**的: screen_cap = SPEED_CEIL_MIB / max(screen_wave) ----
    # v2.3.33。审查方在上轮报「8MiB 撞顶在本出口 0/6, 不发生」, 这一轮它把并发降到 1 再测:
    # 67 / 80.0 / 80.0 / 69 —— 一半撞顶。两个观测都对, 差在档位: `SCREEN_WORKERS=4` 把每路压到
    # ~21MiB/s, 结构上永远碰不到 80 的天花板, **并发分摊把撞顶掩盖了**。
    # 本地取证(NAS samples.jsonl 3 条 / 143 个粗筛分数): P50 21.6 / P90 35.7 / max 62.7 / 到 80 的 0 个。
    # 于是 `screen_capped` 在现行档位下恒空 —— 而"空"有两种含义(这一档撞不到 / 真没人撞), 同形。
    # 这是 P1-B「缺席原因必须可辨」的第四遍, 前三遍钉在 quick_skip / l0_pool / screen_skip+zero 上。
    _smp82 = x78.build_sample("Cn1", 1.0, [], [(-10.0, "1.1.1.1")], {}, {},
                              screen_wave={"1.1.1.1": 4, "2.2.2.2": 1},
                              screen_floor=80.0, screen_need=320.0)
    _smp82b = x78.build_sample("Cn1", 1.0, [], [], {}, {}, screen_wave={},
                               screen_floor=None, screen_need=None)
    _ceil82 = getattr(x78, "SPEED_CEIL_MIB", -1)
    chk("C82", "两个量必须分开记: screen_floor=撞顶线本身(80.0, **与并发无关**) + "
               "screen_need=达到它所需的出口总带宽(80×max(wave)=320.0); 没量出 wave 时都记 None",
        abs(_ceil82 - 80.0) < 1e-9
        and _smp82.get("screen_floor") == 80.0
        and _smp82.get("screen_need") == 320.0
        and _smp82.get("screen_wave") == {"1.1.1.1": 4, "2.2.2.2": 1}
        and "screen_floor" in _smp82b and _smp82b.get("screen_floor") is None,
        "SPEED_CEIL_MIB=%r floor=%r need=%r need_no_wave=%r"
        % (_ceil82, _smp82.get("screen_floor"), _smp82.get("screen_need"),
           _smp82b.get("screen_need")))

    # ---- C83 常量定义**与消费点**都必须是每路口径 ----
    # 反面: 谁"顺手"把 SPEED_CEIL_MIB 除以并发数当成"节点上限", 4 路下线变 20MiB/s, 于是
    # 一半以上节点"被判撞顶"。capped 判据若写成 `el < SPEED_MIN_EL * SCREEN_WORKERS`,
    # 8MiB 在 0.4s 内读完也算撞顶。
    # v2.3.34: 上一版只查**定义行**(`SPEED_CEIL_MIB = ...`), 而错误实际出在**消费点**
    # (`_cap_line = SPEED_CEIL_MIB / _wave_max`) —— `_wave_max` 真机恒等于 SCREEN_WORKERS,
    # 换个变量名就绕过了正则。断言盯住定义、漏掉消费, 是"同一论证写两遍漏一遍"的第五遍。
    _src83 = open(os.path.join(d, "xbox-speedtest.py"), encoding="utf-8").read()
    _m83 = re.search(r"^SPEED_CEIL_MIB\s*=\s*([^\n#]+)", _src83, re.M)
    _expr83 = (_m83.group(1).strip() if _m83 else "")
    _m83b = re.search(r"^\s*_floor_line\s*=\s*([^\n#]+)", _src83, re.M)
    _expr83b = (_m83b.group(1).strip() if _m83b else "")
    chk("C83", "SPEED_CEIL_MIB 是每路天花板: **定义式**不得出现 SCREEN_WORKERS, "
               "**消费点** `_floor_line` 只能是 SPEED_CEIL_MIB 本身(除以任何并发量都是把"
               "『更难达到』记成『门槛下降』); capped 判据是 `el < SPEED_MIN_EL`, 不是 ×并发数",
        bool(_m83) and "SCREEN_WORKERS" not in _expr83
        and "SCREEN_CHUNK" in _expr83 and "SPEED_MIN_EL" in _expr83
        and abs(_ceil82 - 80.0) < 1e-9
        and _expr83b == "SPEED_CEIL_MIB"
        and re.search(r'"capped":\s*el\s*<\s*SPEED_MIN_EL\b', _src83) is not None,
        "定义=%r 值=%r 消费点=%r" % (_expr83, _ceil82, _expr83b))

    # ---- C84 端到端: test_pool 把「撞顶线」和「达到它所需的出口带宽」分开算进样本 ----
    # C82 只证明 build_sample 收得下; 生产路径上这两个数由 test_pool 算 —— 若那里写成
    # `_floor_line = SPEED_CEIL_MIB / _wave_max`(v2.3.33 的真实缺陷), 样本会记 20.0, 于是
    # "分数超过自己的上限"又回来了, 而 C82 仍绿。两条必须都有。
    x84 = load(os.path.join(d, "xbox-speedtest.py"), "xst84")
    _p84 = os.path.join(tempfile.mkdtemp(), "p84.txt")
    with open(_p84, "w", encoding="utf-8") as f:
        f.write("1.1.1.1\t(广东 电信)\n2.2.2.2\t(广东 移动)\n")
    x84.load_ips = lambda p: [("1.1.1.1", "(广东 电信)"), ("2.2.2.2", "(广东 移动)")]

    def _sc84(rs, h, u, budget=None, ms_of=None, capped_out=None, skip_out=None,
              zero_out=None, wave_out=None):
        if wave_out is not None:
            wave_out.update({"1.1.1.1": 4, "2.2.2.2": 3})
        return [(30.0, "1.1.1.1"), (20.0, "2.2.2.2")]
    x84._screen = _sc84
    x84.median_quick = lambda ip, h, u: 40.0
    _sink84 = []
    _r84, _o84 = capture(x84.test_pool, "Cn1", _p84, "h", "/u", None, _sink84)
    _smp84 = _sink84[-1] if _sink84 else {}
    chk("C84", "test_pool 分开算: screen_floor 恒 80.0(与 wave 无关) + screen_need=80×max(wave)=320.0; "
               "日志用**实测最高单路×wave**反推出口下界, 与 need 一比就说清 capped 为什么空",
        _smp84.get("screen_floor") == 80.0
        and _smp84.get("screen_need") == 320.0
        and any("需出口 ≥ 320MiB/s" in ln for ln in _o84.splitlines())
        and any("出口至少 120" in ln for ln in _o84.splitlines()),
        "floor=%r need=%r 相关行=%r"
        % (_smp84.get("screen_floor"), _smp84.get("screen_need"),
           [ln.strip() for ln in _o84.splitlines() if "撞顶" in ln][:2]))

    # ---- C85 真样本不变式: 任何分数都不得超过 screen_floor ----
    # 这是**定义蕴含**的, 不含任何"均分"假设: capped 判据是 el < SPEED_MIN_EL, 分数 = 8MiB/el,
    # 所以 score > 80 在算术上不存在。2026-11-08 04:00 的真样本违反了这一条 —— Cn1 的 48 个分数里
    # 41 个 > screen_cap(20.0), 而 screen_capped 是空的。那不是"数据坏了", 是 20.0 从来没当过撞顶线。
    # 只判**新格式**(有 screen_floor 的行): v2.3.33 及更早的行只有 screen_cap, 按旧口径确实违反,
    # 历史数据不改写, 但新样本必须满足 —— 否则下一个人会照旧读反。
    def _scan85(path):
        """扫一个 samples.jsonl: 返回 (新格式行数, 旧格式行数, 违反列表, 文件在否, 最高分, 最低分)。"""
        if not os.path.isfile(path):
            return 0, 0, [], False, 0.0, 0.0
        _n = _no = 0
        _bad = []
        _mx = _mn = 0.0
        for _ln in open(path, encoding="utf-8"):
            _ln = _ln.strip()
            if not _ln:
                continue
            try:
                _r = json.loads(_ln)
            except Exception:
                continue
            if not isinstance(_r, dict):
                continue
            if "screen_floor" not in _r:
                if "screen_cap" in _r:
                    _no += 1
                continue
            _fl = _r.get("screen_floor")
            _sc = _r.get("screen") or []
            if not _fl or not _sc:
                continue
            _n += 1
            _row = [float(x[0]) for x in _sc]
            _m = max(_row)
            if _m > _fl + 1e-6:
                _bad.append((_r.get("ts"), _r.get("group"), _m, _fl))
            _mx = max(_mx, _m)
            _mn = min(_row) if not _mn else min(_mn, min(_row))
        return _n, _no, _bad, True, _mx, _mn

    _sp85 = os.path.join(d, "samples.jsonl")
    _n85, _n85old, _bad85, _has85, _mx85, _mn85 = _scan85(_sp85)
    # v2.3.35 (审查方 N5): 无样本时 `_bad85 = []` -> `not _bad85` 恒真 -> 绿。本机(无
    # samples.jsonl)就是空过, 只打出"新格式 0 行判定", 结论照样是绿的 —— 空集永远满足不变式。
    # 先喂一个**必然违反**的合成样本, 证明这条判据本身会红, 再去判真样本: 绿必须来自"判据活着
    # 且没抓到", 不能来自"没东西可判"。
    _fx85 = os.path.join(tempfile.mkdtemp(prefix="smp85_"), "samples.jsonl")
    with open(_fx85, "w", encoding="utf-8") as _fw85:
        _fw85.write(json.dumps({"ts": "synthetic", "group": "Cn1", "screen_floor": 80.0,
                                "screen": [[90.0, "1.1.1.1"], [20.0, "2.2.2.2"]]}) + "\n")
        _fw85.write(json.dumps({"ts": "synthetic", "group": "Cn2", "screen_floor": 80.0,
                                "screen": [[73.2, "3.3.3.3"]]}) + "\n")
    _fn85, _fo85, _fb85, _fh85ok, _fmx85, _fmn85 = _scan85(_fx85)
    _alive85 = (_fh85ok and _fn85 == 2 and len(_fb85) == 1
                and _fb85[0][2] == 90.0 and _fmx85 == 90.0 and _fmn85 == 20.0)
    chk("C85", "真样本不变式: 分数 <= screen_floor(定义蕴含: capped=el<0.1s, 分数=8MiB/el, "
               ">80 算术上不存在; 违反即说明 floor 记错了, 与『均分』无关)。判据先用合成样本自证会红, "
               "无真样本时才允许通过",
        _alive85 and not _bad85,
        "判据自检: 合成 2 行 -> 抓 %r (需恰好 1 条 90.0, 最高/最低 %.1f/%.1f); "
        "真样本 %d 行(最高 %.1f 最低 %.1f) / 旧格式(screen_cap) %d 行不判 -> 违反 %r"
        % ([b[2] for b in _fb85], _fmx85, _fmn85, _n85, _mx85, _mn85, _n85old, _bad85[:3]))
    # 观测(不判定): 分数 vs 并发档位。
    # v2.3.36 (审查方 M1): 原来这里写的是"逐 IP 的 wave 没入库, 相关性算不出来", 这是**事实错误** ——
    # `screen_wave` 一直是 `{ip: 进场时同批几路}` 逐 IP 落盘的, replay 也早就配对取出来了。
    # 算不出相关系数只因为**单行内 wave 常无方差**(满编时 48 个 IP 全是 4), 必须跨行按档位攒。
    # 所以这里按 wave 分档给中位数/极差: 档位一多, "同 IP 跨档位分数变不变"就能直接读出来 ——
    # 那正是账上"4 路分摊"需要的观测, 而不是不可测量。
    _bw = {}
    if _n85:
        for _ln in open(_sp85, encoding="utf-8"):
            _ln = _ln.strip()
            if not _ln:
                continue
            try:
                _r = json.loads(_ln)
            except Exception:
                continue
            if not isinstance(_r, dict) or "screen_floor" not in _r:
                continue
            _wv = _r.get("screen_wave") or {}
            for _sp, _ip in (_r.get("screen") or []):
                _w = _wv.get(_ip)
                if isinstance(_w, int) and _w > 0:
                    _bw.setdefault(_w, []).append(float(_sp))
    if _n85 and _mn85 > 0:
        print("  [obs] 真样本 %d 行: 单路分数 %.1f ~ %.1f (floor 80.0) —— 贴 80 说明出口没被打满"
              % (_n85, _mn85, _mx85), flush=True)
    if _bw:
        _parts = []
        for _w in sorted(_bw):
            _v = sorted(_bw[_w])
            _md = _v[len(_v) // 2] if len(_v) % 2 else (_v[len(_v) // 2 - 1] + _v[len(_v) // 2]) / 2.0
            _parts.append("wave=%d n=%d 中位%.1f 极差%.1f~%.1f" % (_w, len(_v), _md, _v[0], _v[-1]))
        print("  [obs] 分数 vs 并发档位(跨 %d 行累积): %s" % (_n85, " | ".join(_parts)), flush=True)
        if len(_bw) == 1:
            print("       只有 1 个档位 —— 不下结论(这就是『要攒』的意思: 档位一多才看得出同 IP 变不变)",
                  flush=True)
    elif _n85:
        print("  [obs] %d 行新格式样本里没有 screen_wave 能与 screen 配对 —— 这一列先攒数据" % _n85,
              flush=True)

    # ---- C93 逐 IP 的 wave **确实**入库(replay 2c 的分档观测全靠它) ----
    # v2.3.36 (审查方 M1): v2.3.35 在三处注释里写了"逐 IP 的 wave 没入库", 是事实错误, 而且方向
    # 致命 —— "没入库" = 结构上永远不可能, 账上挂着的"4 路分摊"会被判成不可测量; 真相是数据一直
    # 在(`screen_wave` = {ip: 进场时同批几路}), 缺的只是跨档位样本。这条把事实钉在三处:
    #   ①build_sample 真的是按 ip 建 dict(不是只存一个 max) ②replay 有分档观测段
    #   ③合成样本上 screen 的每个 IP 都能在 screen_wave 里配对(这正是 replay 的配对口径)
    _s93 = open(os.path.join(d, "xbox-speedtest.py"), encoding="utf-8").read()
    # replay.py 从**审计器同目录**读(与上面 C78 的 _rp78 同口径): 变异目录里没有 tests/
    # (selftest 的 FILES 只复制顶层交付物), 写 `d/tests/replay.py` 会在基线就 FileNotFoundError ——
    # 那是"自检自己坏了", 比漏网更难看出来。
    _r93 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "replay.py"),
                encoding="utf-8").read()
    _fx93 = os.path.join(tempfile.mkdtemp(prefix="smp93_"), "samples.jsonl")
    with open(_fx93, "w", encoding="utf-8") as _fw93:
        _fw93.write(json.dumps({"ts": "synthetic", "group": "Cn1", "screen_floor": 80.0,
                                "screen": [[73.2, "1.1.1.1"], [66.6, "2.2.2.2"]],
                                "screen_wave": {"1.1.1.1": 4, "2.2.2.2": 3}}) + "\n")
    _o93 = json.loads(open(_fx93, encoding="utf-8").readline())
    _wv93 = _o93.get("screen_wave") or {}
    _pair93 = all(_ip in _wv93 for _sp, _ip in _o93["screen"]) and len(_wv93) == 2
    chk("C93", "逐 IP 的 wave 确实入库: build_sample 的 screen_wave 是 {ip: wave} 逐 IP 字典、"
               "replay 有『分数 vs 并发档位』分档段、合成样本上 screen 的 IP 全能在 screen_wave "
               "配对 —— 『没入库』这个说法不再成立(它会让 4 路分摊被判成不可测量)",
        ('"screen_wave": dict(' in _s93)
        and ("[obs] 分数 vs 并发档位" in _r93)
        and _pair93,
        "源码按 ip 建 dict=%r replay 分档段=%r 合成配对=%r(wave=%r)"
        % ('"screen_wave": dict(' in _s93, "[obs] 分数 vs 并发档位" in _r93, _pair93, _wv93))

    # ---- C94 同一秒两次隔离: 两份坏证据都要在 ----
    # v2.3.36 (审查方 M3): `.corrupt-<int(ts)>` 只到秒, 同一秒内隔离两次会**覆盖前一份坏证据**。
    # 概率极低(一轮 3405s), 但坏证据被悄悄覆盖这件事事后无法发现 —— 所以加序号。
    x94 = load(os.path.join(d, "xbox-speedtest.py"), "xst94")
    _d94 = tempfile.mkdtemp(prefix="res94_")
    _p94 = os.path.join(_d94, "results-dup.json")
    _s94 = [{"group": "Cn1", "ip": "9.9.9.9", "speed": 95.0}]
    _k94 = ["Cn1", "Cn2"]
    _ev94 = []
    for _step in (1, 2):
        with open(_p94, "w", encoding="utf-8") as _fw94:
            _fw94.write("{坏%d" % _step)
        x94.persist_results(_p94, _s94, _k94, ts=1700000010)
        _ev94.append([f for f in os.listdir(_d94) if ".corrupt-1700000010" in f])
    _bak94 = sorted(set(_ev94[0]) | set(_ev94[1]))
    _txt94 = [open(os.path.join(_d94, f), encoding="utf-8").read() for f in _bak94]
    chk("C94", "同一秒两次隔离: 两份证据都在(文件名带序号, 内容各是当次的坏字节), 不互相覆盖",
        len(_bak94) == 2 and sorted(_txt94) == ["{坏1", "{坏2"],
        "隔离文件=%r 内容=%r" % (_bak94, _txt94))

    # ---- C86 results.json 合并: 三条静默丢一组的路全部堵住 ----
    # 审查方 P1-B: C13 验读侧(损坏 -> 空表, 那是**对的**)、C22 验写侧(seed 是合法 Cn2),
    # 两条各自全绿, 而**交叉点**没人测 —— 一次 `--pool Cn1` 定向重测就能把 Cn2 整条抹掉,
    # 代价是 load_record 回到 0.0(劣化档永久关闭)与 load_hotspares 回到 [](止血没备胎), 且不吭声。
    x86 = load(os.path.join(d, "xbox-speedtest.py"), "xst86")
    _s86 = [{"group": "Cn1", "ip": "9.9.9.9", "speed": 95.0}]
    _c86 = [("Cn1", ""), ("Cn2", "")]
    _k86 = [p[0] for p in _c86]
    _r86a, _ok86a = x86.merge_results(
        [{"group": "Cn1", "ip": "1.1.1.1"}, {"group": "Cn2", "ip": "2.2.2.2"}], _s86, _k86)
    _r86b, _ok86b = x86.merge_results({"group": "Cn2", "ip": "2.2.2.2"}, _s86, _k86)
    _r86c, _ok86c = x86.merge_results("不是 list", _s86, _k86)
    _r86d, _ok86d = x86.merge_results(
        [{"group": "Cn2", "ip": "2.2.2.2"}, {"group": "Cn3", "ip": "3.3.3.3"}, "垃圾"], _s86, _k86)
    _r86e, _ok86e = x86.merge_results([], _s86, _k86)
    chk("C86", "results.json 合并: ①只测一组时另一组必须留着 ②顶层不是 list / 不是数组 -> readable=False"
               "(调用方不得写回, 拿 [] 当'没有旧账'就是丢数据的那条路) ③POOLS 之外的组原地保留 "
               "④表里的非 dict 元素跳过而不炸 ⑤首次部署(空表)正常合并",
        [g.get("group") for g in _r86a] == ["Cn1", "Cn2"]
        and _ok86a is True
        and _ok86b is False and _r86b == []
        and _ok86c is False
        and [g.get("group") for g in _r86d] == ["Cn1", "Cn2", "Cn3"]
        and [g.get("group") for g in _r86e] == ["Cn1"],
        "①%r ②ok=%r ③%r ④%r ⑤%r"
        % ([g.get("group") for g in _r86a], _ok86b,
           [g.get("group") for g in _r86d], _ok86d,
           [g.get("group") for g in _r86e]))

    # ---- C86b 坏表**连跑两轮**: 第二轮必须自愈 ----
    # v2.3.35 (审查方 N1): v2.3.34 的处置是"读不出 -> 这一轮不写回", 而全仓只有那一处写点 ——
    # 于是坏文件永远不会被重写: load_record 恒 0.0(劣化档永久关闭)、load_hotspares 恒 [](没备胎),
    # 且这个状态**自我维持**。C86 当时五条全绿, 恰恰因为它验的是"不覆盖", 而"不覆盖"正是成因。
    # 所以这条必须连跑两轮: 第一轮隔离 + 重建, 第二轮走正常合并(还能吃回别的组) —— 只验第一轮,
    # 验不出这条永久故障。
    _tmp86 = tempfile.mkdtemp(prefix="res86_")
    _p86 = os.path.join(_tmp86, "results.json")
    _corrupt86 = '{坏 json'
    with open(_p86, "w", encoding="utf-8") as _fw86:
        _fw86.write(_corrupt86)
    _m86a, _st86a = x86.persist_results(_p86, _s86, _k86, ts=1700000000)
    try:
        _after1 = json.load(open(_p86, encoding="utf-8"))
    except Exception:
        _after1 = None
    _bak86 = sorted(f for f in os.listdir(_tmp86) if ".corrupt-" in f)
    _bak86_txt = open(os.path.join(_tmp86, _bak86[0]), encoding="utf-8").read() if _bak86 else None
    # 第二轮: 旧表已可读, 手工塞进 Cn2(模拟另一组上轮留下的结论), 本轮仍然只测 Cn1
    _seed86 = [g for g in (_after1 or []) if isinstance(g, dict)]
    _seed86.append({"group": "Cn2", "ip": "2.2.2.2", "speed": 66.0})
    with open(_p86, "w", encoding="utf-8") as _fw86b:
        json.dump(_seed86, _fw86b)
    _m86b, _st86b = x86.persist_results(_p86, _s86, _k86, ts=1700000001)
    try:
        _after2 = json.load(open(_p86, encoding="utf-8"))
    except Exception:
        _after2 = None
    # 首次部署(文件不存在)是正常态, 不能被当成"损坏"而报 rebuilt
    _p86e = os.path.join(_tmp86, "results-fresh.json")
    _m86c, _st86c = x86.persist_results(_p86e, _s86, _k86, ts=1700000002)
    # ④ v2.3.36 (审查方 M2): created 不能用"文件不在"当判据 —— 上一轮 unrepaired 之后文件
    # 也是不在的(坏表已被 os.replace 挪去隔离, 新文件又没写成), 两者完全同形。status 值本身
    # 有损, 与 P3-F 的 canary bool 同病, 只是代价在日志。造这条链: ①写成功一次(留下 .seen)
    # ②删掉 results.json(等价于"隔离挪走 + 没写成") ③再跑 -> 必须是 created_after_writefail。
    _p86d = os.path.join(_tmp86, "results-wf.json")
    x86.persist_results(_p86d, _s86, _k86, ts=1700000003)
    _seen86 = os.path.isfile(_p86d + ".seen")
    os.remove(_p86d)
    _m86d, _st86d = x86.persist_results(_p86d, _s86, _k86, ts=1700000004)
    chk("C86b", "results.json 坏表**连跑两轮**: ①第一轮 status=rebuilt、坏字节被隔离成 .corrupt-<ts>"
                "(内容原样) 、文件已可读且只含本轮组 ②第二轮 status=ok 走正常合并并吃回别的组 "
                "③首次部署(无文件)= created 而不是 rebuilt "
                "④写过一次后文件不见了(上一轮写了没成功) = created_after_writefail, 不是 created",
        _st86a == "rebuilt"
        and bool(_bak86) and _bak86_txt == _corrupt86
        and [g.get("group") for g in (_after1 or [])] == ["Cn1"]
        and _st86b == "ok"
        and sorted(g.get("group") for g in (_after2 or [])) == ["Cn1", "Cn2"]
        and _st86c == "created" and os.path.isfile(_p86e)
        and _seen86 and _st86d == "created_after_writefail" and os.path.isfile(_p86d + ".seen"),
        "①st=%r bak=%r 内容一致=%r 文件=%r ②st=%r 组=%r ③st=%r ④seen=%r st=%r"
        % (_st86a, _bak86, _bak86_txt == _corrupt86,
           [g.get("group") for g in (_after1 or [])],
           _st86b, [g.get("group") for g in (_after2 or [])], _st86c, _seen86, _st86d))

    # ---- C91 候选截断排序: 三态不得压回同一个键 ----
    # v2.3.35 (审查方 P2-5): `score.get(ip, 0.0)` 让"预算跳过没测到"与"测了 0 分"等价,
    # 而两者物理含义相反(未知 vs 已知坏)。这里直接喂纯函数, 不比行为反推。
    _score91 = {"a": 50.0, "b": 20.0}
    _skip91 = {"d"}
    _order91 = sorted(["c", "a", "d", "b"], key=lambda i: x86.cand_rank(i, _score91, _skip91))
    _k91 = [x86.cand_rank(i, _score91, _skip91) for i in ("a", "d", "c")]
    chk("C91", "候选排序三态: 有分按分降序 > 没测到(预算跳过, 未知) > 测了 0 / 不在粗筛表 —— "
               "把 skip 与 zero 压回同一个键会让截断先扔掉'未知'节点",
        _order91 == ["a", "b", "d", "c"]
        and _k91[0][0] < _k91[1][0] < _k91[2][0],
        "order=%r keys=%r" % (_order91, _k91))

    # ---- C92 decision_note 的极差与中位数必须同口径 ----
    # v2.3.35 (审查方 N3): `median_of` 去 0(0 = 这一轮没测出), 而 min/max 吃含 0 的 rs ——
    # 0/45/47 会打出「受抖动支配(三轮 0.0~47.0 横跨阈值)」, 把"一轮没测出来"说成"抖动"。
    # 这两件事处置相反: 抖动该等下一轮, 没测出该查网络/查节点。
    x92 = load(os.path.join(d, "xbox-speedtest.py"), "xst92")
    x92._STEADY_ROUNDS_MEM = {"1.1.1.1": [0.0, 45.0, 200.0], "2.2.2.2": [0.0, 0.0, 0.0],
                              "3.3.3.3": [0.0, 40.0, 90.0]}
    _n92a = x92.decision_note("1.1.1.1", 40.0)     # 出数轮 45/200 都过线 -> 稳健, 不能叫抖动
    _n92b = x92.decision_note("2.2.2.2", 40.0)     # 一轮都没出数 -> 不出声(零值日志另说)
    _n92c = x92.decision_note("3.3.3.3", 60.0)     # 出数轮 40~90 真横跨 -> 该叫抖动
    chk("C92", "decision_note 极差口径 = 中位数口径(只用出数轮): 0/45/200 vs 阈值 40 应判稳健且"
               "不出现'0.0~'; 全 0 不出声; 40/90 横跨阈值才叫受抖动支配",
        "稳健" in _n92a and "0.0~" not in _n92a
        and _n92b == ""
        and "受抖动支配" in _n92c and "40.0~90.0" in _n92c,
        "①%r ②%r ③%r" % (_n92a, _n92b, _n92c))

    # ---- C87 两组同轮止血到同一节点: 跨组 warn 必须响 ----
    # 审查方 P1-C: `quick_fix(...)` 的返回值被丢掉, 于是 `pins` 还停在 get_pins() 那一刻的值,
    # 第二组算 other_pins 时看到的是第一组的**旧** IP。两组切到同一个节点, 而那条
    # 「单节点故障会同时打掉两组」的 warn 一次都不会响 —— 它比的是"对方上一轮钉着谁"。
    m87 = load(wd_p, "wd87")
    m87.BASE = tempfile.mkdtemp()
    m87.get_pins = lambda: {"Cn1": "1.1.1.1", "Cn2": "2.2.2.2"}
    m87.steady = lambda ip, host=None, path=None: 99.0 if ip == "7.7.7.7" else 0.0   # 两组死, 备胎好
    m87.load_hotspares = lambda n: [{"ip": "7.7.7.7", "steady": 99}]   # 两组备胎是同一个
    m87.router_cmd = lambda cmd: ("" if "show" not in cmd else
                                  "dhcp.@dnsmasq[0].address='/assets1.xboxlive.cn/1.1.1.1' "
                                  "dhcp.@dnsmasq[0].address='/assets2.xboxlive.cn/2.2.2.2'")
    m87.subprocess = type("S", (), {"run": staticmethod(lambda *a, **k: None)})()
    _r87, _o87 = capture(m87.main)
    _warn87 = [ln for ln in _o87.splitlines() if "同时是另一组的钉住 IP" in ln]
    chk("C87", "两组同轮都止血到同一个热备: 必须打跨组 warn(单节点故障会同时打掉两组)。"
               "这要求 quick_fix 的返回值被调用点接住 —— 否则 other_pins 比的是对方**旧** IP, 永远不响",
        len(_warn87) >= 1 and _o87.count("止血成功 => 切 7.7.7.7") == 2,
        "warn 条数=%d 切到 7.7.7.7 次数=%d\n%s"
        % (len(_warn87), _o87.count("止血成功 => 切 7.7.7.7"), _o87[-400:]))

    # ---- C88 冷却带组维度: 一组的重测不得压住另一组 ----
    # 审查方 P2-D: 标记只有一个 epoch, 08:30 修了 Cn1, 09:00 Cn2 整组死, 09:30 只止血不重测, 最长压 3 小时。
    m88 = load(wd_p, "wd88")
    m88.BASE = tempfile.mkdtemp()
    with open(os.path.join(m88.BASE, m88.RETEST_MARK_NAME), "w", encoding="utf-8") as f:
        f.write(json.dumps({"Cn1": int(time.time() - 60)}))     # 只压 Cn1
    _left88a = m88.retest_cooldown_left("Cn1")
    _left88b = m88.retest_cooldown_left("Cn2")
    with open(os.path.join(m88.BASE, m88.RETEST_MARK_NAME), "w", encoding="utf-8") as f:
        f.write("%d\n" % int(time.time() - 60))                 # 旧格式: 一格管全部
    _left88c = m88.retest_cooldown_left("Cn2")
    m88.get_pins = lambda: {"Cn1": "1.1.1.1", "Cn2": "2.2.2.2"}
    m88.steady = lambda ip, host=None, path=None: 0.0
    m88.load_hotspares = lambda n: []
    m88.router_cmd = lambda cmd: ("" if "show" not in cmd else
                                  "dhcp.@dnsmasq[0].address='/assets1.xboxlive.cn/1.1.1.1' "
                                  "dhcp.@dnsmasq[0].address='/assets2.xboxlive.cn/2.2.2.2'")
    _ran88 = []
    m88.subprocess = type("S", (), {"run": staticmethod(lambda *a, **k: _ran88.append(a))})()
    with open(os.path.join(m88.BASE, m88.RETEST_MARK_NAME), "w", encoding="utf-8") as f:
        f.write(json.dumps({"Cn1": int(time.time() - 60)}))     # 再写回"只压 Cn1"
    _r88, _o88 = capture(m88.main)
    _trig88 = [ln for ln in _o88.splitlines() if "[trigger]" in ln]
    chk("C88", "重测冷却按组判: 标记只压 Cn1 时 Cn2 必须照常触发(Cn1 仍被压); "
               "**旧格式(裸数字)仍对全部组生效** —— 格式升级瞬间宁可多压一轮, 也不能放过本该冷却的那次",
        _left88a > 0 and _left88b == 0 and _left88c > 0
        and any("--pool Cn2" in ln and "--pool Cn1" not in ln for ln in _trig88),
        "Cn1剩=%r Cn2剩=%r 旧格式Cn2剩=%r trigger=%r"
        % (_left88a, _left88b, _left88c, _trig88[:2]))

    # ---- C89 分段耗时必须进样本(不能再只打给人看) ----
    # 审查方 P2-E: `_t` 在落盘前被 pop, 样本只有 t_l0; 而 t_l0 写进每一行却从未被正文读过。
    # 3405s 一轮, 事后算不出"哪一段在变慢"。
    _smp89 = x78.build_sample("Cn1", 5.0, [], [], {}, {}, t_screen=7.0, t_quick=32.0)
    _smp89b = x78.build_sample("Cn1", 5.0, [], [], {}, {}, )
    chk("C89", "分段耗时进样本: t_screen / t_quick 与 t_l0 三段都在(只打日志的话, 30 天后回放算不出"
               "哪一段在变慢); 没传时记 None, 与『记了 0』可辨",
        _smp89.get("t_screen") == 7.0 and _smp89.get("t_quick") == 32.0
        and _smp89.get("t_l0") == 5.0
        and "t_screen" in _smp89b and _smp89b.get("t_screen") is None,
        "t_l0=%r t_screen=%r t_quick=%r (无参时 t_screen=%r)"
        % (_smp89.get("t_l0"), _smp89.get("t_screen"), _smp89.get("t_quick"),
           _smp89b.get("t_screen")))

    # ---- C90 canary 不得再有 bool 返回值(它是 state 的有损副本) ----
    # 审查方 P3-F: `return False` 同时代表 gone / denied / unknown / failed 四件事。决策早已只走
    # state, 于是这是个"看着像判据、实际会骗人"的死接口 —— 下一个人写 `if not canary(x):`
    # 就把"404 撤版"和"本机断网"当成同一件事, 正是三档注释花力气防的错。
    # 用 AST 而不是正则: 要确认**每一个** Return 都不带值, 而不是"某一行看起来没有 return True"。
    _tree90 = ast.parse(open(wd_p, encoding="utf-8").read())
    _fn90 = None
    for _n90 in _tree90.body:
        if isinstance(_n90, ast.FunctionDef) and _n90.name == "canary":
            _fn90 = _n90
            break
    _vals90 = [ast.dump(r.value) for r in ast.walk(_fn90) if isinstance(r, ast.Return) and r.value is not None] \
        if _fn90 else ["<无 canary 函数>"]
    _call90 = [n for n in ast.walk(_tree90)
               if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "canary"]
    # "丢掉返回值"= 调用点作为一条独立语句(ast.Expr.value)。别的位置都是"当值用",
    # 那就等于重新引入一个判据口 —— 正是这条要防的。用位置集合判, 不靠 attrs 猜测。
    _bare90 = set()
    for _n90b in ast.walk(_tree90):
        if isinstance(_n90b, ast.Expr) and isinstance(_n90b.value, ast.Call) \
                and getattr(_n90b.value.func, "id", "") == "canary":
            _bare90.add(id(_n90b.value))
    _used90 = [ast.dump(n)[:120] for n in _call90 if id(n) not in _bare90]
    chk("C90", "canary 只写 state、不返回 bool(False 同时代表 gone/denied/unknown/failed, "
               "决策只走 state; 留一个 bool 就是留一个会骗人的判据)",
        _vals90 == [] and not _used90,
        "带值的 return=%r 调用点被当值用=%r" % (_vals90, _used90))


def _stub_wd(wd_p):
    """给 watchdog.main 搭桩: 按 IP 队列返回吞吐序列, 记录是否触发重测"""
    class S:
        def __init__(self):
            self.m = load(wd_p, "wd")
            self.m.BASE = tempfile.mkdtemp()
            self.pins = {}
            self.q = {}
            self.out = ""
            self.ran = []

        def set_pins(self, p):
            self.pins = p

        def seq(self, ip, vals):
            self.q[ip] = list(vals)

        def run(self):
            m = self.m

            def steady(ip, host=None, path=None):
                q = self.q.get(ip) or [100.0]
                return q.pop(0) if len(q) > 1 else q[0]
            m.get_pins = lambda: self.pins
            m.steady = steady
            m.load_record = lambda n, ip=None: 100.0
            m.load_hotspares = lambda n: []
            m.quick_fix = lambda n, ip, floor=0, other_pins=(): None
            m.router_cmd = lambda cmd: ""
            self.ran = []

            class P:
                rc = 3          # 故意非 0: 让"退出码必须进日志"这条能被断言抓到(见 C62)
                @staticmethod
                def run(*a, **k):
                    self.ran.append((a, k))
                    return type("R", (), {"returncode": P.rc})()
            m.subprocess = P()

            # v2.3.31: `main` 现在每小时真的问一次 `canary_gate()`。不桩 http 的话, **每一条**
            # 走 main 的用例都会对真 URL 发 HEAD: 有网时是几十毫秒, 没网/被墙时每条卡满
            # `DL_TIMEOUT=12s` —— 这里八条用例就是 96s, 而 NAS 侧 M86(把 canary 改成绑 IP)
            # 的实测结果是 `subprocess.TimeoutExpired: ... timed out after 300 seconds`。
            # 审计必须能离线跑, 这条桩和 A22 是一个道理: 不能靠"本机恰好能连上"。
            class _Resp(object):
                status = 206            # 200/206 = 活: 让 canary 静默, 决策链不受它影响
                def close(self):
                    pass

            class _Conn(object):
                def __init__(self, host=None, *a, **k):
                    pass
                def request(self, method, path, headers=None):
                    pass
                def getresponse(self):
                    return _Resp()
                def close(self):
                    pass

            class _Client(object):
                HTTPConnection = _Conn
                HTTPSConnection = _Conn
            m.http = type("_HTTP", (object,), {"client": _Client})
            m.BindIPConn = _Conn       # canary 另一条出门的路, 有一条没桩住就等于没桩

            _r, self.out = capture(m.main)
            return self.out
    return S()


# ------------------------------------------------------------- D 交付卫生
# 通用特征: 不写任何真实凭据字面量(把密码当"检测关键词"写进仓库等于换个位置泄漏一次)。
# 真凭据串走本地私密清单: 环境变量 XBOX_AUDIT_DENY="串1,串2" 或
# 文件 ~/.xbox_audit_deny.txt(一行一条, 不入库), 审计时合并进来。
DENY_PATTERNS = [
    (r"(?i)\b(?:pass|passwd|password|pwd)\s*[:=]\s*\S{6,}", "明文口令赋值"),
    (r"(?<!\d)1[3-9]\d{9}(?!\d)", "手机号"),
    (r"\b192\.168\.\d{1,3}\.\d{1,3}\b", "私网 IP(192.168.x.x)"),
    (r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "私网 IP(10.x)"),
    (r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b", "私网 IP(172.16-31)"),
    (r"/home/[A-Za-z0-9_.-]+/", "Linux 用户目录路径"),
    (r"[A-Za-z]:\\\\?Users\\\\?", "Windows 用户目录路径"),
    (r"\bAdministrator\b", "本机用户名"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "私钥"),
]

# 通用占位符白名单: 路由器出厂默认地址, 不是任何人的组网指纹。
# 个人真实内网地址不得进这个名单 —— 那正是要去掉的东西。
ALLOW_LITERALS = {"192.168.1.1"}


def deny_list():
    """真实凭据串: 环境变量优先, 其次 ~/.xbox_audit_deny.txt; 都没有就只用通用特征"""
    out = [s for s in os.environ.get("XBOX_AUDIT_DENY", "").split(",") if s.strip()]
    p = os.path.expanduser("~/.xbox_audit_deny.txt")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            out += [l.strip() for l in fh if l.strip() and not l.startswith("#")]
    return out


def audit_hygiene(d):
    print("\n--- D 交付卫生 ---")
    files = ["xbox-speedtest.py", "watchdog.py", "pool_refresh.py", readme_name(d)]
    blob = ""
    for f in files:
        p = os.path.join(d, f)
        raw = open(p, "rb").read()
        blob += raw.decode("utf-8", "replace")
        chk("D-crlf:" + f, "%s 为 LF 换行(部署到 Linux 不炸)" % f, b"\r\n" not in raw)
    # deliver.sh 也是交付物(2026-11-07 新增, 只在 F 侧存在 —— NAS 没有对端, 所以这里必须 isfile 守)。
    # 之前这四件全审、独漏它: 一个 CRLF 的 deliver.sh 在 git-bash 里第一行就 command not found,
    # 而 137 项照样全绿 —— 交付脚本自己不在交付面的审计里, 是"交付态自己不看自己"的最后一层。
    _dlv = os.path.join(d, "deliver.sh")
    if os.path.isfile(_dlv):
        _draw = open(_dlv, "rb").read()
        chk("D-crlf:deliver.sh", "deliver.sh 为 LF 换行(CRLF 会让 git-bash 报 command not found)",
            b"\r\n" not in _draw)
        _dtxt = _draw.decode("utf-8", "replace")
        # 锚点必须是 `PAIRS=(` .. 行首 `)`。曾写成 `PAIRS=$(.*?)\n$`: 那个 `$` 在正则里是**行尾锚点**,
        # 等于要求「PAIRS= 之后直接结束」, 永远匹配不上 -> _pl 恒为 [] -> 这条检查永久红(实测)。
        _blk = re.search(r"PAIRS=\((.*?)\n\)", _dtxt, re.S)
        _pl = re.findall(r'^\s*"([^":]+):', _blk.group(1), re.M) if _blk else []
        # 漏一行 => 那个文件在 NAS 上永远是旧版, 而 check 打 ok; 多一行 => 路径打错, 永远报 DIFF。
        # v2.3.31: `tests/replay.py` 就是漏在这一行里的 —— README 清单列了它、PAIRS 没有,
        # 于是 push 永远不带它、而它自己在 NAS 上永远缺; D8 却因为 `_want` 也只数两个 tests 而全绿。
        # 交付集合从这个常量走, 别再手抄一份。
        _want = set(files) | {"tests/audit.py", "tests/selftest_audit.py", "tests/replay.py",
                              "../AI共管台账.md"}
        chk("D8", "deliver.sh 的 PAIRS 与交付集合一一对应(漏一行=那个文件在 NAS 上永远是旧版而 check 说 ok)",
            bool(_blk) and set(_pl) == _want, "PAIRS=%s 多=%s 少=%s"
            % (sorted(_pl), sorted(set(_pl) - _want), sorted(_want - set(_pl))))
    bad = []
    for pat, label in DENY_PATTERNS:
        hits = []
        for m in re.finditer(pat, blob):
            h = m.group(0)
            if h in ALLOW_LITERALS:
                continue
            # 网段定义(RFC1918/RFC6598/RFC5737)本身不是组网指纹: 规范里就那么写, 谁家都一样。
            # 只放过**紧跟 /NN** 的写法 —— 写真实主机地址时不会带这个后缀, 所以这是收紧而不是放宽。
            if re.match(r"/\d{1,2}(?![\d.])", blob[m.end():]):
                continue
            hits.append(h)
        if hits:
            bad.append("%s×%d" % (label, len(hits)))
    extra = deny_list()
    for s in extra:
        if s in blob:
            bad.append("私密清单命中(长度 %d)" % len(s))
    chk("D1", "四件产物无凭据/私网指纹/个人路径", not bad,
        "命中=%s 私密清单条数=%d" % (bad, len(extra)))
    rd = open(os.path.join(d, readme_name(d)), encoding="utf-8").read()
    chk("D2", "README 文件列表覆盖三个脚本", all(f in rd for f in files[:3]))
    chk("D3", "README 声明了已知局限段(而不是只写优点)", "## 已知局限" in rd and len(
        re.findall(r"^- ", rd.split("## 已知局限")[1].split("##")[0], re.M)) >= 4,
        "条数=%d" % len(re.findall(r"^- ", rd.split("## 已知局限")[1].split("##")[0], re.M)))
    chk("D4", "三脚本均声明 utf-8 编码(cron 环境 locale 可能不是 UTF-8)",
        all("# -*- coding: utf-8 -*-" in open(os.path.join(d, f), encoding="utf-8").read()
            for f in files[:3]))
    chk("D5", "审计器与测试脚本不含真实凭据(检测规则/夹具不得变成泄漏载体)",
        not [s for s in extra if _grep_tree(os.path.join(d, "tests"), s)]
        and not _grep_tree(os.path.join(d, "tests"), "PRIVATE KEY"),
        "私密清单=%d 条" % len(extra))
    # v2.3.7: 流量账必须写在文档里 —— 2026-11-06 网卡计数器实测一轮全量 9.58GiB、看门狗 0.5GiB/小时,
    # 这些数字此前只活在注释里, 而且注释还是错的(称哨兵"约 160MB", 实测 309MB)
    chk("D6", "README 写明流量口径: 单次稳态字节上限 + 单轮全量流量 + 每小时哨兵流量",
        "256MiB" in rd and "GiB" in rd and re.search(r"0\.\d+\s*GiB", rd) is not None
        and "小时" in rd,
        "256MiB=%s GiB=%s 小时=%s" % ("256MiB" in rd, "GiB" in rd, "小时" in rd))
    # 稳态窗口的真实限制有三个(时间/字节/文件大小), 只写前两个会让人以为调大字节上限就能测满 8 秒
    chk("D7", "README 说明测速文件自身大小是第三个上限(Cn1 只有 295.4MiB)",
        "295.4MiB" in rd and "文件" in rd, "295.4MiB=%s" % ("295.4MiB" in rd))


def _grep_tree(root, needle):
    """在目录下 .py 里找字面量(只用于私密清单/私钥这类真凭据, 不用通用正则:
    tests/ 里的夹具本来就是故意造的坏字符串)。
    跳过审计器自身 —— 它的正则定义里必然包含这些词的片段。"""
    if not os.path.isdir(root):
        return []
    hits = []
    for dirpath, _dirs, fs in os.walk(root):
        for f in fs:
            if not f.endswith(".py") or f == "audit.py":
                continue
            p = os.path.join(dirpath, f)
            if needle in open(p, encoding="utf-8", errors="replace").read():
                hits.append(os.path.relpath(p, root))
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--print-exits", action="store_true")
    a = ap.parse_args()
    d = os.path.abspath(a.dir)
    if a.print_exits:
        for fn in ("xbox-speedtest.py", "watchdog.py", "pool_refresh.py"):
            for k, v in sorted(collect_failure_exits(os.path.join(d, fn)).items()):
                if v:
                    print('    ("%s", "%s"): %d,' % (fn, k, v))
        return 0
    print("审计目录: %s" % d)
    # 每一段都必须自己兜底: 断言里被测代码一旦抛异常(比如契约被改到函数不可调用),
    # 审计器不能整体崩掉 —— 崩掉只会看到一个 traceback、FAIL 列表是空的, 变异自检会误判"漏网"。
    # 崩溃本身就是最严重的一种红, 要落成一条 FAIL 记录。
    # v2.3.19: `actual` 必须在这里初始化。原来只在 B 段返回后才赋值(`if fn is
    # audit_failure_registry`), B 段一抛异常就 `continue`, 走到下面的
    # `len(actual)` 变成 UnboundLocalError —— 审计器在最该出声的时候(有断言崩了)
    # 反而吐一个 traceback、连 RESULT 行都没有, 变异自检会把"漏网"当成"通过"。
    # 这正是上面注释里声明"不能发生"的那件事(2026-11-07 外部审计 P0-2)。
    actual = {}
    _prod_before = run_products_state(d)      # A26 交付态守卫: 跑前拍一次
    for seg, fn, cid in (("A 静态契约", audit_static, "A0"),
                         ("B 失败出口棘轮", audit_failure_registry, "B0"),
                         ("C 行为契约", audit_behaviour, "C0"),
                         ("D 交付卫生", audit_hygiene, "D0")):
        try:
            r = fn(d)
        except Exception as e:
            import traceback
            chk(cid, "%s 段可执行(不得因被测代码抛异常而整体崩溃)" % seg, False,
                "%s: %s" % (type(e).__name__, e))
            print("  [trace] %s" % traceback.format_exc().strip().splitlines()[-3:])
            continue
        if fn is audit_failure_registry:
            actual = r or {}      # 只有 B 段有返回值(出口统计), 别被后面几段的 None 覆盖掉
    # A26: 跑完对账。断言的是"文件的字节没变", 不是"用例记得改 BASE" —— 后者靠自觉, 而且
    # 2026-11-07 已经靠不住一次(38 行假样本混进真 samples.jsonl, 人眼在 39 行里挑不出来)。
    _prod_after = run_products_state(d)
    _dirty = [n for n in RUN_PRODUCTS if _prod_before.get(n) != _prod_after.get(n)]
    chk("A26", "审计不写交付态: 跑完 audit 后 samples/results/speed.log/.last_retest/run.log 一字节不动",
        not _dirty, "被改动=%s (before=%s after=%s)"
        % (_dirty, [_prod_before.get(n) for n in _dirty], [_prod_after.get(n) for n in _dirty]))
    fail = [r for r in RESULTS if not r[2]]
    print("\n失败出口统计: %d 个函数 / %d 条出口" % (len(actual), sum(actual.values())))
    print("RESULT: %d 项, 全过" % len(RESULTS) if not fail
          else "RESULT: %d 项, %d 项 FAIL -> %s" % (len(RESULTS), len(fail),
                                                    " ".join(r[0] for r in fail)))
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
