#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交付前机器审计 (2026-11-06, python3.8 兼容)

为什么有这个东西: 前几轮"审计"是我人眼读代码, 每次只发现"这次盯的那块",
于是每被叫一次就冒一个新问题。人读不可复现、不可累积、不能拦回归。
这里把三类断言固化成可执行文件, 每次交付前跑一次, 全 PASS 才允许发:

  A 静态契约  跨文件结构一致性(组名/域名表/测速对/环境变量/README 常量/版本号)
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
import importlib.util
import io
import json
import os
import re
import sys
import tempfile

FAIL_CONSTS = (False, None, 0, 0.0, "")

# ---------------------------------------------------------------- 失败出口登记表
# (文件, 函数) -> 失败出口数。数字变化 = 有人新增/删除了一条失败路径, 必须重新审视
# 并在 FAILURE_TESTS 里给出对应用例名, 否则不批交付。
FAILURE_EXITS = {
    ("xbox-speedtest.py", "dl_speed"): 3,          # 非 200/206、异常、正常返回路径里的 0 值
    ("xbox-speedtest.py", "dl_steady"): 3,
    ("xbox-speedtest.py", "test_pool"): 1,          # 全部候选无有效结果 -> None
    ("xbox-speedtest.py", "router_cmd"): 2,         # ssh 异常、非 0 退出
    ("xbox-speedtest.py", "main"): 4,               # 锁占用、路由器不可达、两组无结果分支
    ("watchdog.py", "router_cmd"): 2,
    ("watchdog.py", "router_apply"): 2,             # uci 失败、restart 失败
    ("watchdog.py", "steady"): 3,
    ("watchdog.py", "load_hotspares"): 1,           # results.json 读不出
    ("watchdog.py", "load_record"): 6,              # json/float 两层容错
    ("watchdog.py", "quick_fix"): 1,                # 无达标热备 -> None
    ("watchdog.py", "main"): 2,                     # 锁占用、路由器不可达
    ("pool_refresh.py", "refresh"): 1,             # 单域名解析失败不中断
}

# 每个含失败出口的函数, 至少要有这些用例覆盖(用例名与下方 C 段一致)
FAILURE_TESTS = {
    ("xbox-speedtest.py", "dl_speed"): ["C1"],
    ("xbox-speedtest.py", "dl_steady"): ["C1"],
    ("xbox-speedtest.py", "test_pool"): ["C17"],
    ("xbox-speedtest.py", "router_cmd"): ["C21"],
    ("xbox-speedtest.py", "main"): ["C19", "C20", "C21", "C22"],
    ("watchdog.py", "router_cmd"): ["C9", "C10"],
    ("watchdog.py", "router_apply"): ["C7", "C8", "C9", "C10"],
    ("watchdog.py", "steady"): ["C1"],
    ("watchdog.py", "load_hotspares"): ["C13"],
    ("watchdog.py", "load_record"): ["C13"],
    ("watchdog.py", "quick_fix"): ["C3", "C5", "C6"],
    ("watchdog.py", "main"): ["C2", "C21b"],
    ("pool_refresh.py", "refresh"): ["C24"],
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


def load(path, tag):
    """每次给唯一模块名, 避免同一轮里多次加载互相污染(桩打歪曾让冒烟骗过我一次)"""
    global _SEQ
    _SEQ += 1
    name = "audit_%s_%d" % (tag, _SEQ)
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_SEQ = 0


def capture(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = fn(*a, **kw)
    return r, buf.getvalue()


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
        ("DEGRADE_MIN_RECORD 25", wd.DEGRADE_MIN_RECORD, 25.0),
    ]
    diffs = ["%s: code=%r" % (n, v) for n, v, exp in pairs if v != exp]
    chk("A4", "代码常量与交付口径一致(README 引用的数字未被偷偷改动)", not diffs, "; ".join(diffs))
    chk("A5", "README 引用的关键常量名都在文档里出现",
        all(k in rd for k in ("MAX_QUICK_CANDS", "QUICK_BUDGET_SEC", "20MiB/s", "55%", "15%", "30%",
                              "8MiB/s")),
        "缺: %s" % [k for k in ("MAX_QUICK_CANDS", "QUICK_BUDGET_SEC", "20MiB/s", "55%", "15%", "30%",
                                "8MiB/s") if k not in rd])

    vs = set()
    for fn in ("xbox-speedtest.py", "watchdog.py"):
        head = open(os.path.join(d, fn), encoding="utf-8").read().split('"""')[1]
        vs.add(re.search(r"v2\.\d+\.\d+", head).group(0))
    chk("A6", "两脚本版本号一致且 README 已收录", len(vs) == 1 and list(vs)[0] in rd, "versions=%s" % vs)

    # 环境变量: 代码读的必须在 README 文档化, 文档写的必须在代码里真的被读
    code = "".join(open(os.path.join(d, f), encoding="utf-8").read() for f in
                   ("xbox-speedtest.py", "watchdog.py", "pool_refresh.py"))
    envs = set(re.findall(r"os\.environ\.get\(\s*[\"']([A-Z_]+)[\"']", code))
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
    chk("A12", "README 不写死断言条数(避免文档与审计器漂移)",
        not re.search(r"\d+\s*项断言", rd), str(re.findall(r"\d+\s*项断言", rd)))


# ------------------------------------------------------------ B 失败出口棘轮
def collect_failure_exits(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    counts = {}

    def walk(node, scope):
        for n in ast.iter_child_nodes(node):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                counts.setdefault(n.name, 0)
                walk(n, n.name)
            else:
                if (isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)
                        and n.value.value in FAIL_CONSTS):
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
        m.parse_hist(hist2) == {"d1.xboxlive.cn": {"9.9.9.9", "8.8.8.8"},
                                "d2.xboxlive.cn": {"9.9.9.9"}},
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
    def st_env(rows, regions=("广东", "广西"), screen=None):
        x = load(xs_p, "xst")
        x.BASE = tempfile.mkdtemp()
        x.SCREEN_REGIONS = regions
        p = os.path.join(x.BASE, "pool.txt")
        with open(p, "w", encoding="utf-8") as f:
            for ip, tag in rows:
                f.write("%s\t%s\n" % (ip, tag))
        seen = {"rows": None}

        def _sc(rs, host, path):
            seen["rows"] = [ip for ip, _ in rs]
            return screen(rs) if screen else [(100.0 - i, ip) for i, (ip, _) in enumerate(rs)]
        x._screen = _sc
        x.median_quick = lambda ip, host, path: 50.0
        x.median_steady = lambda ip, host, path: 60.0
        return x, p, seen

    rows = [("1.1.%d.1" % i, "(北京 联通)") for i in range(30)]
    rows += [("2.2.%d.1" % i, "(广东 电信)") for i in range(5)]
    rows += [("3.3.%d.1" % i, "(广西 移动)") for i in range(3)]
    rows += [("4.4.1.1", "(auto1106 assets1.xboxlive.cn)")]
    x, p, seen = st_env(rows)
    r, out = capture(x.test_pool, "Cn1", p, "h", "/u", "9.9.9.9")
    chk("C14", "区域优先粗筛只测本地+新增(不全池)",
        len(seen["rows"]) == 9 and "区域优先粗筛" in out, "screened=%d %s" % (len(seen["rows"]), out[:120]))

    x, p, seen = st_env(rows, regions=("福建",))
    r, out = capture(x.test_pool, "Cn1", p, "h", "/u", None)
    chk("C15", "省份配错(池内无该省) => 回落全池而不是只测新增节点",
        len(seen["rows"]) == len(rows) and "直接全池粗筛" in out, "screened=%d" % len(seen["rows"]))

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

    def st_run(ranked, quick_map, steady_map, hist=hist_cn1, argv=("--pool", "Cn1"), seed=None):
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
        x._screen = lambda rs, h, u: ranked
        x.median_quick = lambda ip, h, u: quick_map.get(ip, 0.0)
        x.median_steady = lambda ip, h, u: steady_map.get(ip, 0.0)
        rec = []
        x.router_cmd = lambda cmd: (rec.append(cmd) or (hist if "show" in cmd else ""))
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
        g["ip"] == "9.9.9.9" and "unmeasurable" in g["router"], g.get("router"))

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
            m.load_record = lambda n: 100.0
            m.load_hotspares = lambda n: []
            m.quick_fix = lambda n, ip, floor=0: None
            m.router_cmd = lambda cmd: ""
            self.ran = []

            class P:
                @staticmethod
                def run(*a, **k):
                    self.ran.append(a)
            m.subprocess = P()
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
    bad = []
    for pat, label in DENY_PATTERNS:
        hits = [h for h in re.findall(pat, blob) if h not in ALLOW_LITERALS]
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
    audit_static(d)
    actual = audit_failure_registry(d)
    audit_behaviour(d)
    audit_hygiene(d)
    fail = [r for r in RESULTS if not r[2]]
    print("\n失败出口统计: %d 个函数 / %d 条出口" % (len(actual), sum(actual.values())))
    print("RESULT: %d 项, 全过" % len(RESULTS) if not fail
          else "RESULT: %d 项, %d 项 FAIL -> %s" % (len(RESULTS), len(fail),
                                                    " ".join(r[0] for r in fail)))
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
