#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审计器自检 (变异测试, python3.8 兼容)

一个永远 PASS 的审计毫无价值。这里把交付物复制到临时目录, 逐个人为注入
已知缺陷(mutant), 断言 audit.py 必须变红、且红在预期的那条检查上。

  M1 常量漂移      MAX_QUICK_CANDS 16->17 (文档/代码口径不一致)      -> A4
  M2 新增失败出口  quick_fix 多一条 return None                      -> B1
  M3 凭据/私网泄漏 注释里塞一个私网 IP (夹具, 非真实组网)          -> D1
  M4 域名表缺项    DOMAINS["Cn1"] 丢掉 d2.xboxlive.cn                -> A2
  M5 逻辑回归      劣化阈值 0.55 -> 0.30 (掉七成才动手)              -> C1
  M6 谎报成功      router_apply 去掉 restart 失败判定                -> C9

用法: python3 tests/selftest_audit.py        退出码 0 = 审计器可信
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)
AUDIT = os.path.join(HERE, "audit.py")
FILES = ["xbox-speedtest.py", "watchdog.py", "pool_refresh.py", "nas-README.md"]

MUTANTS = [
    ("M1", "常量漂移 MAX_QUICK_CANDS 16->17", "xbox-speedtest.py", "A4",
     "MAX_QUICK_CANDS = 16", "MAX_QUICK_CANDS = 17"),
    ("M2", "新增失败出口 (quick_fix 多一条 return None)", "watchdog.py", "B1",
     '                print(f"{ts()} [{name}] 热备 {ip} 达标但 uci 写入/重启失败, 试下一个", flush=True)\n',
     '                print(f"{ts()} [{name}] 热备 {ip} 达标但 uci 写入/重启失败, 试下一个", flush=True)\n'
     '                return None\n'),
    ("M3", "凭据/私网指纹泄漏", "watchdog.py", "D1",
     "# 注1: mp 下载域名已改原生 DNS 解析(不钉 IP),不在看门狗管辖内",
     "# 注1: mp 下载域名已改原生 DNS 解析(不钉 IP),不在看门狗管辖内 192.168.99.7"),
    ("M4", "域名表缺项 (Cn1 丢掉 d2)", "watchdog.py", "A2",
     '"Cn1": ("assets1.xboxlive.cn", "assets2.xboxlive.cn", "d1.xboxlive.cn", "d2.xboxlive.cn"),',
     '"Cn1": ("assets1.xboxlive.cn", "assets2.xboxlive.cn", "d1.xboxlive.cn"),'),
    ("M5", "逻辑回归 劣化阈值 0.55->0.30", "watchdog.py", "C1",
     "DEGRADE_RATIO = 0.55", "DEGRADE_RATIO = 0.30"),
    ("M6", "谎报成功 (restart 失败仍返回 True)", "watchdog.py", "C9",
     '        print(f"{ts()} [router] dnsmasq restart 失败(uci 已提交但 DNS 未生效, 需人工复核)", flush=True)\n        return False',
     '        print(f"{ts()} [router] dnsmasq restart 失败(uci 已提交但 DNS 未生效, 需人工复核)", flush=True)'),
]


def run_audit(d):
    p = subprocess.run([sys.executable, AUDIT, "--dir", d],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def main():
    tmp = tempfile.mkdtemp(prefix="mutant_")
    fails = []
    try:
        # 基线: 原样复制必须全过(否则后面所有判断都没意义)
        base = os.path.join(tmp, "pristine")
        os.makedirs(base)
        for f in FILES:
            shutil.copy(os.path.join(SRC, f), base)
        rc, out = run_audit(base)
        ok = rc == 0 and "全过" in out
        print(("PASS" if ok else "FAIL") + " 基线: 未经修改的交付物审计全过")
        if not ok:
            fails.append("baseline")
            print(out[-1500:])

        for mid, desc, fn, expect_cid, old, new in MUTANTS:
            d = os.path.join(tmp, mid)
            os.makedirs(d)
            for f in FILES:
                shutil.copy(os.path.join(SRC, f), d)
            p = os.path.join(d, fn)
            src = open(p, encoding="utf-8").read()
            if old not in src:
                print("FAIL %s %s | 注入点不存在, 自检本身失效" % (mid, desc))
                fails.append(mid)
                continue
            open(p, "w", encoding="utf-8", newline="\n").write(src.replace(old, new, 1))
            rc, out = run_audit(d)
            red = re.findall(r"^FAIL (\S+)", out, re.M)
            caught = rc == 1 and expect_cid in red
            print(("PASS" if caught else "FAIL") + " %s %s | 审计红在 %s (实际红: %s)"
                  % (mid, desc, expect_cid, ",".join(red) or "无"))
            if not caught:
                fails.append(mid)
                print(out[-800:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nSELFTEST RESULT:", "审计器可信(6/6 缺陷都被抓到)" if not fails else "有漏网: %s" % fails)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
