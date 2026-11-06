#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Xbox 加速 IP 池刷新 (NAS 手动运行, python3.8):
  对 6 个 CDN 域名做 DNS 解析, 新出现的 A 记录追加进池(带 autoMMDD 标签),
  auto1105 类标签的节点会进入 speedtest 的区域优先粗筛。
  用途: 池子长期不刷新会错过新边缘节点; DNS 解析无新 IP 时输出 appended 0 属正常。
用法: python3 pool_refresh.py
注: 写入逻辑在 main() 内并由 __main__ 保护 —— 被 import 时不得有副作用
    (旧版写在模块级, 任何 import 都会真的解析 DNS 并改池文件)
"""
import datetime
import os
import socket

BASE = os.path.dirname(os.path.abspath(__file__))  # 与另两个脚本同目录, 无需手改
TAG = "auto" + datetime.date.today().strftime("%m%d")

POOLS = {
    "IP.XboxCn1.txt": ["assets1.xboxlive.cn", "assets2.xboxlive.cn", "d1.xboxlive.cn", "d2.xboxlive.cn"],
    "IP.XboxCn2.txt": ["dlassets.xboxlive.cn", "dlassets2.xboxlive.cn"],
}


def refresh(pools=None, tag=None, base=None):
    """解析并追加新节点, 返回 {文件名: [新增IP]}; 单域名解析失败只跳过该域名"""
    pools = POOLS if pools is None else pools
    tag = TAG if tag is None else tag
    base = BASE if base is None else base
    added = {}
    for f, doms in pools.items():
        path = os.path.join(base, f)
        existing = set()
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                existing = {l.split()[0] for l in fh if l.strip()}
        new = []
        for d in doms:
            try:
                for info in socket.getaddrinfo(d, 80, socket.AF_INET):
                    ip = info[4][0]
                    if ip not in existing and ip not in [x[0] for x in new]:
                        new.append((ip, d))
            except Exception as e:
                print(f, "resolve", d, "ERR", e)
        if new:
            with open(path, "a", encoding="utf-8") as fh:
                for ip, d in new:
                    fh.write("%s\t(%s %s)\n" % (ip, tag, d))
        added[f] = [ip for ip, _ in new]
        print(f, "appended", len(new), "->", " ".join(ip for ip, _ in new))
    return added


def main():
    refresh()


if __name__ == "__main__":
    main()
