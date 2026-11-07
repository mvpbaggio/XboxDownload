#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Xbox 加速 IP 池刷新 (NAS 手动运行, python3.8):
  对 6 个 CDN 域名做 DoH 解析, 新出现的 A 记录追加进池(带 autoMMDD 标签)。
  标签只是"这行是哪天、从哪个域名捞来的"记号: v2.3.26 起 speedtest 的候选来源是实测 RTT 排序,
  不再按标签筛 —— 新节点能不能进粗筛只看它 443 握手多快, 不看标签。
用法: python3 pool_refresh.py
注: 写入逻辑在 main() 内并由 __main__ 保护 —— 被 import 时不得有副作用
    (旧版写在模块级, 任何 import 都会真的解析 DNS 并改池文件)

v2.3.25 (2026-11-07 第十八轮): 写入前校验 A 记录(F2)。这条路径此前**零校验** —— 唯一一道
  门是 existing 去重, 也就是"只要不重复就往池子里写"。而 DoH 的 Answer[].data 是**上游给的字符串**:
  端点被污染/中间设备改包/上游自己出错, 0.0.0.0、127.0.0.1、192.168.x.x、100.64.x.x 都会原样进来。
  一旦写进池子, 下游三件事全拿它当节点: load_ips 收下(点分正则会认)、粗筛白等一个超时、
  稳态 0 被读成"节点已死"; 最坏被 router_apply 钉进 dnsmasq —— 那一步的后果不是"慢", 是**整组断网**。
  现在只放行**公网单播 IPv4**, 其余逐条打 reject + 理由(和 resolved/appended 同一行口径, 差额能对上)。
  为什么不用 ipaddress 的 is_private 一刀切: py3.8 里 100.64/10(CGNAT) 的 is_private 是 **False**,
  而 NAS 恰好就是 3.8.10 —— 版本相关属性做网段判定, 等于"本地绿、NAS 绿得不一样"。所以显式写网段表。

v2.3.14 (2026-11-07): 解析方式 socket.getaddrinfo -> DoH(https://dns.alidns.com/resolve)。
  这个脚本此前**结构性失效, 一个节点都追不回来**, 而日志一直说"appended 0 属正常":
  这 6 个域名全在路由器 dnsmasq 的钉 IP 名单里(`address=/assets1.xboxlive.cn/<ip>`),
  NAS 上对**任何** 53 端口解析器(默认 / 223.5.5.5 / 114.114.114.114 / 8.8.8.8 / 119.29.29.29)
  查这 6 个域名, 回来的都是**当前钉住的那个 IP 本身**(连 :: NODATA 特征都一起带),
  也就是说查询根本没到公网递归。getaddrinfo 永远只能拿到已在池里的地址 -> appended 永远 0。
  2026-11-07 实测: 池文件 mtime 停在 10-13 22:44, 冻结 25 天。
  443 端口不受 53 劫持, 实测 DoH 能拿到真实 A 记录(Cn1 新增 7 个 / Cn2 新增 5 个池外边缘节点)。
  顺带改了日志口径: 现在打 "resolved N | appended M" —— 原来的 appended 0 分不清
  "池子本来最新" 与 "一个都没解析到", 后者才是这次冻结 25 天没被任何人发现的原因。
"""
import datetime
import ipaddress
import json
import os
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))  # 与另两个脚本同目录, 无需手改
TAG = "auto" + datetime.date.today().strftime("%m%d")
DOH_ENDPOINT = "https://dns.alidns.com/resolve"
DOH_TIMEOUT = 10.0

POOLS = {
    "IP.XboxCn1.txt": ["assets1.xboxlive.cn", "assets2.xboxlive.cn", "d1.xboxlive.cn", "d2.xboxlive.cn"],
    "IP.XboxCn2.txt": ["dlassets.xboxlive.cn", "dlassets2.xboxlive.cn"],
}

# 只放行公网单播 IPv4。刻意**不用** ipaddress 的 is_private/is_reserved 一刀切:
# 那些属性随 Python 版本变(3.8 里 100.64/10 的 is_private 是 False, 3.13 才是 True),
# 而 NAS 是 3.8.10、本地是 3.13 —— 用版本相关属性做安全判定, 结果就是"两边都绿, 绿的不是同一件事"。
_REJECT_NETS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8",          # "本网络": 连它等于连本机, 下载必失败
    "10.0.0.0/8",
    "100.64.0.0/10",      # CGNAT
    "127.0.0.0/8",
    "169.254.0.0/16",     # 链路本地(含云元数据 169.254.169.254)
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",       # 文档用, 不会是真节点
    "192.168.0.0/16",
    "198.18.0.0/15",      # 基准测试用
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",        # 组播
    "240.0.0.0/4",        # 保留 + 255.255.255.255 广播
))


def ip_reject_reason(s):
    """返回 None = 可以写进池子; 返回一句人话原因 = 拒收(进日志)。

    这里的 None 是"放行", 不是失败; 但失败出口棘轮(B1)不分语义, 照样把它登记一条 ——
    这样下一个人想加一条"静默放行"的分支, 数量会变、B1 立刻红。
    """
    try:
        a = ipaddress.ip_address((s or "").strip())
    except ValueError:
        return "解析不出 IP"
    if a.version != 4:
        return "不是 IPv4"
    for n in _REJECT_NETS:
        if a in n:
            return "非公网段 %s" % n
    return None


def doh_a(name):
    """DoH 查 A 记录(走 443, 不受本机 53 端口钉 IP 劫持)。

    返回 [] = 这次拿到的是只有 CNAME、没有 A 的回答, 与"查询失败"由异常区分:
    前者是上游这次没给 A(同一域名连查三次都可能不同), 后者说明 DoH 出口坏了 —— 两者都 appended 0,
    但不该同样处理。**别把这个当成某域名的固有属性**: 2026-11-07 我先测到 dlassets2 一次空回答,
    就写成"该域名没有 A 记录", 一小时后再查它返回了 2 个 Akamai 节点(23.53.6.108/107)。
    """
    q = urllib.parse.urlencode({"name": name, "type": "A"})
    req = urllib.request.Request(DOH_ENDPOINT + "?" + q, headers={"User-Agent": "xbox-pool-refresh/2"})
    with urllib.request.urlopen(req, timeout=DOH_TIMEOUT) as r:
        j = json.load(r)
    if j.get("Status", 0) != 0:      # 0=NOERROR, 3=NXDOMAIN
        return []
    return [a["data"] for a in (j.get("Answer") or []) if a.get("type") == 1 and a.get("data")]


def refresh(pools=None, tag=None, base=None):
    """DoH 解析并追加新节点, 返回 {文件名: [新增IP]}; 单域名查询失败只跳过该域名"""
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
        resolved = 0
        rejected = []
        seen_bad = set()
        for d in doms:
            try:
                ips = doh_a(d)
            except Exception as e:
                print(f, "doh", d, "ERR", e)
                continue
            if not ips:
                print(f, "doh", d, "无 A 记录, 跳过")
                continue
            resolved += len(ips)
            for ip in ips:
                why = ip_reject_reason(ip)
                if why:
                    # 逐条打: resolved 里已经算进去了(上游确实答了), 但没写进池子 ——
                    # 这两个数必须是能对上的差额, 否则又回到"appended 0 说不清"那种账
                    if ip not in seen_bad:
                        seen_bad.add(ip)
                        rejected.append((ip, why))
                        print(f, "doh", "reject", repr(ip), why)
                    continue
                if ip not in existing and ip not in [x[0] for x in new]:
                    new.append((ip, d))
        if new:
            with open(path, "a", encoding="utf-8") as fh:
                for ip, d in new:
                    fh.write("%s\t(%s %s)\n" % (ip, tag, d))
        added[f] = [ip for ip, _ in new]
        # 必须同时打 resolved: 光有 appended 0 分不清"池子最新"与"一个都没解析到"(后者冻结了 25 天没人发现)
        # rejected 同理: appended 0 也可能是"答了 12 条, 12 条全不是公网 IPv4"
        print(f, "resolved", resolved, "| appended", len(new), "| rejected", len(rejected),
              "->", " ".join(ip for ip, _ in new))
    return added


def main():
    refresh()


if __name__ == "__main__":
    main()
