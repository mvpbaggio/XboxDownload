#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Xbox 加速 - NAS 定时自动测速找最快节点并更新路由器 (ZOS/Python3.8 兼容)
v2.3.4 (2026-11-06):
  - 交付流程整改: 引入 tests/audit.py 机器审计(静态契约/失败出口棘轮/行为契约/交付卫生),
    本脚本逻辑本身未改 —— 此前"每次人眼审一遍就冒一个新问题", 改为可重复的断言
v2.3.3 (2026-11-06):
  - 清理: 删除已被 steady_top_diverse 取代的死函数 steady_top(留着易误用: 它没有运营商分散/可用性下限)
v2.3.2 (2026-11-06):
  - 修 bug: 快测阶段无总时长约束, 本地保送把候选堆到 31 个, 每个龟速候选 2 轮×25s,
    实测一次定向重测烧 1496s(25 分钟) -> MAX_QUICK_CANDS=16(按粗筛分截断, 当前IP必留) + QUICK_BUDGET_SEC=420 总预算
  - 修 bug: 预算提前退出时 quick 字典缺键导致下游 KeyError -> 结果按 quick.items() 取
  - 改进: 热备名单按运营商分散(steady_top_diverse)。2026-11-06 20:30 三个热备全是广东电信,
    同一段故障同时掉到 1.0/1.4MiB/s, 止血退化成全量重测 -> 至少留一条别的运营商的活路
v2.3.1 (2026-11-06):
  - 路由器 IP/省份改环境变量(去隐私化); 修 watchdog POOLS 3 元组解包崩溃; pool_refresh 目录自动定位
v2.3 (2026-11-05):
  - 韧性: dl_speed 加 25s 墙钟上限(原来只有 dl_steady 有 cap, 龟速节点可把串行快测拖成小时级)
  - 修 bug: 运行锁把 fcntl ImportError(非Linux)误报为"已有实例运行" -> 分开处理
  - 修 bug: dnsmasq restart 失败被静默吞掉 -> 打日志提示人工复核
v2.2 (2026-11-05):
  - 修 bug: --pool 定向重测覆盖另一组 results.json 热备名单 -> 按组合并 + 原子写(tmp+rename)
  - 修 bug: SCREEN_EXTRA_TAG 硬编码 auto1105, 池刷新次日(auto1106)新增节点进不了粗筛 -> autoMMDD 正则
  - 修 bug: 路由器 ssh 不可达时不再白跑全量测速(直接跳过)
  - 瘦身: results.json 不再落 rows/tag_of 全池明细
v2.1 (2026-11-05):
  - 移除网段黑名单(用户拍板: 谁快用谁, 节点死亡由看门狗热备止血兜底, 不预先排除任何网段)
v2 (2026-11-05):
  - 区域优先粗筛: 只粗筛 本地省份(默认 广东/广西, 可用 XBOX_SCREEN_REGIONS 配)+新增节点, 空结果回退全池
    (省份可配; 全池 248+244 个 IP 粗筛是触发轮 3.2h 长跑的主因)
  - 分组迟滞: WIN_RATIO = {Cn1:1.15, Cn2:1.30} (Cn2 晚间高峰波动大, 稳赢 30% 才换)
  - 热备名单: results.json 每组附 hotspares(冠军+实测过的次优), 供看门狗止血
  - --pool 参数: 只测指定组(看门狗触发定向重测)
流程:
  1) 区域优先 8MB 粗筛(并发4) -> Top8
  2) 候选 = Top8 + 本地省份保送 + 当前路由器IP(按粗筛分降序, 封顶 MAX_QUICK_CANDS); 80MB 单连接快测(2轮中位数)排序
  3) 冠军候选 vs 当前IP: 8s 稳态下载复测(3轮中位数), 冠军稳态需赢迟滞阈值才换节点
     (稳态复测摊平 CDN 突发, 解决 Cn2/dlassets 域"快测虚高")
  4) 更新: uci 幂等(del 全部历史值再 add 新值, 防 address 堆积) + dnsmasq restart
  5) 结果/日志写入 results.json + speed.log (每次追加一行)
用法: python3 xbox-speedtest.py [--pool Cn1 [--pool Cn2]]
路由器免密: 依赖 ~/.ssh/id_ed25519_router 已授权
"""
import json
import os
import re
import socket
import statistics
import subprocess
import sys
import time
import http.client
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.abspath(__file__))
POOLS = [
    ("Cn1", os.path.join(BASE, "IP.XboxCn1.txt"), "assets1.xboxlive.cn",
     "/Z/routing/extraextralarge.txt",
     ["assets1.xboxlive.cn", "assets2.xboxlive.cn", "d1.xboxlive.cn", "d2.xboxlive.cn"]),
    ("Cn2", os.path.join(BASE, "IP.XboxCn2.txt"), "dlassets.xboxlive.cn",
     "/public/content/1b5a4a08-06f0-49d6-b25f-d7322c11f3c8/372e2966-b158-4488-8bc8-15ef23db1379/1.5.0.1018.88cd7a5d-f56a-40c7-afd8-85cd4940b891/ACUEU771E1BF7_1.5.0.1018_x64__b6krnev7r9sf8",
     ["dlassets.xboxlive.cn", "dlassets2.xboxlive.cn"]),
]
# 注: mp 下载域名(dl.delivery.mp.microsoft.com 等)不再钉 IP——2026-10-27 WorkBuddy 证实
# 钉 IP 对 HTTPS 消费端(Edge)会因证书不匹配/节点过期导致下载失败,该组改原生 DNS 解析。


SCREEN_CHUNK = 8 * 1024 * 1024
QUICK_CHUNK = 80 * 1024 * 1024
QUICK_MAX_SEC = 25.0          # 快测/粗筛墙钟上限: 防龟速节点把 80MB 拖成无限长(网络劣化时触发重测正需要它)
STEADY_MAX_SEC = 8.0          # 稳态下载最长 8s(文件读完即止)
SCREEN_WORKERS = 4
DL_TIMEOUT = 12.0
QUICK_ROUNDS = 2              # 快测 2 轮中位数(仅用于排序选冠军候选)
MAX_QUICK_CANDS = 16          # 快测候选上限(按粗筛分降序取): 本地保送在省份节点多时会膨胀到 30+,
                              # 每个龟速候选 2 轮×QUICK_MAX_SEC, 30 个候选实测拖到 1496s
QUICK_BUDGET_SEC = 420.0      # 快测阶段总墙钟预算: 超了就用已测结果(单次 cap 封不住总时长)
STEADY_ROUNDS = 3             # 稳态复测 3 轮中位数(决策依据)
WIN_RATIO = {"Cn1": 1.15, "Cn2": 1.30}   # 分组迟滞: Cn2 晚间波动大, 稳赢 30% 才换
SCREEN_REGIONS = tuple(filter(None, os.environ.get(
    "XBOX_SCREEN_REGIONS", "广东,广西").split(",")))  # 区域优先粗筛(按所在省份配置,环境变量 XBOX_SCREEN_REGIONS 逗号分隔); 区域无有效结果回退全池
SCREEN_EXTRA_RE = re.compile(r"auto[01]\d{3}")  # pool_refresh 的 autoMMDD 标签(不硬编码日期)
HOTSPARE_N = 3                     # results.json 热备名单上限(含冠军)
SCREEN_N = 8
ROUTER = os.environ.get("XBOX_ROUTER_IP", "192.168.1.1")  # 路由器内网 IP:环境变量 XBOX_ROUTER_IP 覆盖,默认仅通用示例
SSH_KEY = os.path.expanduser("~/.ssh/id_ed25519_router")


def load_ips(path):
    rows = []
    for line in open(path, encoding="utf-8"):
        s = line.strip()
        m = re.match(r"^([0-9.]+)\s+(.*)$", s)
        if m:
            rows.append((m.group(1), m.group(2)))
    return rows


class BindIPConn(http.client.HTTPConnection):
    def __init__(self, host, ip, timeout):
        super().__init__(host, timeout=timeout)
        self._bind_ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._bind_ip, 80), timeout=self.timeout)


def _req(conn, host, path):
    conn.request("GET", path, headers={
        "Host": host, "Range": "bytes=0-167772159",
        "User-Agent": "XboxDownload", "Accept-Encoding": "identity"})


def dl_speed(ip, host, path, chunk):
    """快测: 下载 chunk 字节, 速率 = got/el (CDN 突发会虚高, 仅用于排序)"""
    try:
        conn = BindIPConn(host, ip, DL_TIMEOUT)
        _req(conn, host, path)
        resp = conn.getresponse()
        if resp.status not in (200, 206):
            conn.close()
            return 0.0
        t0 = time.perf_counter()
        got = 0
        while got < chunk:
            if time.perf_counter() - t0 >= QUICK_MAX_SEC:
                break  # 墙钟上限: 龟速节点按已下部分的平均速率计(结果自然垫底)
            b = resp.read(16384)  # 读粒度=cap 检查粒度: read 会阻塞读满, 块越小超限越收敛
            if not b:
                break
            got += len(b)
        el = time.perf_counter() - t0
        conn.close()
        return got / (1048576 * max(el, 0.1))
    except Exception:
        # 单个 IP 异常(断连/超时/不完整响应)只记为 0, 不得拖垮整组
        return 0.0


def dl_steady(ip, host, path, max_sec=STEADY_MAX_SEC):
    """稳态: 持续下载最多 max_sec(文件读完即止), 摊平 CDN 突发带宽"""
    try:
        conn = BindIPConn(host, ip, DL_TIMEOUT)
        _req(conn, host, path)
        resp = conn.getresponse()
        if resp.status not in (200, 206):
            conn.close()
            return 0.0
        t0 = time.perf_counter()
        got = 0
        while True:
            if time.perf_counter() - t0 >= max_sec:
                break
            b = resp.read(65536)
            if not b:
                break
            got += len(b)
        el = time.perf_counter() - t0
        conn.close()
        return got / (1048576 * max(el, 0.1))
    except Exception:
        return 0.0


def median_of(vals):
    good = [v for v in vals if v > 0]
    return statistics.median(good) if good else 0.0


def median_quick(ip, host, path):
    return median_of([dl_speed(ip, host, path, QUICK_CHUNK) for _ in range(QUICK_ROUNDS)])


def median_steady(ip, host, path):
    return median_of([dl_steady(ip, host, path) for _ in range(STEADY_ROUNDS)])


def parse_hist(current):
    """OpenWrt uci 把同一 option 的多个 list 合并一行, 仅首条带 address= 前缀,
    其余为裸引号串 -> 用引号包裹的完整 /domain/ip 全量匹配"""
    hist = {}
    for m in re.finditer(r"'/([^/]+)/([0-9.]+)'", current):
        hist.setdefault(m.group(1), set()).add(m.group(2))
    return hist


def _screen(rows, host, url_path):
    """8MB 粗筛, 返回 [(speed, ip)] 降序"""
    ips = [ip for ip, _ in rows]
    with ThreadPoolExecutor(max_workers=SCREEN_WORKERS) as ex:
        speeds = {ip: r for ip, r in zip(ips, ex.map(lambda i: dl_speed(i, host, url_path, SCREEN_CHUNK), ips))}
    return sorted(((sp, ip) for ip, sp in speeds.items() if sp > 0), reverse=True)


def op_of(tag):
    """从 "(中国广东佛山 电信)" 这类标签里取运营商名, 供热备分散判断"""
    t = (tag or "").replace("(", " ").replace(")", " ").strip()
    return t.split()[-1] if t else ""


def steady_top_diverse(results, exclude, host, url_path, tags, n=2, avoid_ops=(), min_steady=20.0):
    """稳态复测热备, 按运营商分散 + 可用性下限过滤:
    - 分散: 同运营商段级故障时整份名单会一起失效。2026-11-06 20:30 实测三个热备全是广东电信,
      同时掉到 1.0/1.4MiB/s, 止血退化成 25 分钟全量重测 -> 至少留一条别的运营商的活路。
    - 下限: 分散不能拿烂节点凑数(首版曾把 4.3MiB/s 的"别家运营商"塞进名单, watchdog 20MiB/s 门槛过不了,
      纯占位)。低于 min_steady 的一律不进名单, 名单短就是真的没有可用热备。
    只对快测序前 5 个候选做稳态(每个 3 轮×8s), 再多纯烧时间。"""
    measured = []
    for _sp, ip in results:
        if len(measured) >= 5:
            break
        if ip in exclude:
            continue
        measured.append({"ip": ip,
                         "steady": round(median_steady(ip, host, url_path), 1),
                         "_op": op_of(tags.get(ip, ""))})
    measured.sort(key=lambda x: x["steady"], reverse=True)
    usable = [m for m in measured if m["steady"] >= min_steady]
    if measured and not usable:
        print(f"  [warn] 实测 {len(measured)} 个候选稳态均 < {min_steady:.0f}MiB/s, 无可信热备(名单留空)", flush=True)
    out, seen = [], set(avoid_ops)   # avoid_ops: 冠军已占用的运营商, 不与冠军撞车
    for m in usable:                 # 第一轮: 一运营商一个
        if m["_op"] and m["_op"] in seen:
            continue
        out.append(m)
        seen.add(m["_op"])
        if len(out) >= n:
            break
    for m in usable:                 # 第二轮: 按稳态序补满
        if len(out) >= n:
            break
        if m not in out:
            out.append(m)
    for m in out:
        m.pop("_op", None)           # 私有字段不落盘
    # 成员已按运营商分散选好, 排序回到"谁快谁在前": watchdog 逐个试时不能先切到慢的
    out.sort(key=lambda x: x["steady"], reverse=True)
    return out[:n]


def test_pool(name, path, host, url_path, current_ip):
    full_rows = load_ips(path)
    rows = full_rows
    tag_of = {ip: tag for ip, tag in full_rows}
    prov = [(ip, tag) for ip, tag in rows if any(r in tag for r in SCREEN_REGIONS)]
    extra = [(ip, tag) for ip, tag in rows if SCREEN_EXTRA_RE.search(tag)]
    if prov:
        prov_ips = {ip for ip, _ in prov}
        region_rows = prov + [(ip, tag) for ip, tag in extra if ip not in prov_ips]
        print(f"[{name}] 区域优先粗筛: 本地 {len(prov)} + 新增 {len(extra)} = {len(region_rows)}/{len(rows)} 个节点 "
              f"(区域无有效结果自动回退全池)", flush=True)
        ranked = _screen(region_rows, host, url_path)
    else:
        # 池内没有 SCREEN_REGIONS 标记的节点(省份配错/池未标注): 少数 auto 新节点代表不了最快线路, 必须全池
        region_rows = []
        ranked = []
    if not ranked:
        if region_rows:
            print(f"[{name}] 区域/新增节点粗筛无有效结果, 回退全池", flush=True)
        else:
            print(f"[{name}] 池内无 {','.join(SCREEN_REGIONS)} 标记节点, 直接全池粗筛", flush=True)
        ranked = _screen(rows, host, url_path)
    top = [ip for _, ip in ranked[:SCREEN_N]]
    # 本地省份节点保送确认: 粗筛小块+并发会低估本地节点, 防止漏选(本地运营商节点延迟优势最大)
    gd = [ip for ip, tag in (prov or []) if any(r in tag for r in SCREEN_REGIONS)]
    score = dict((ip, sp) for sp, ip in ranked)
    cands = list(dict.fromkeys(top + gd + ([current_ip] if current_ip else [])))
    n_before = len(cands)
    # 按粗筛分降序截断, 当前 IP 必留(它是迟滞基准, 丢了就无法判断"该不该换")
    cands.sort(key=lambda i: score.get(i, 0.0), reverse=True)
    if len(cands) > MAX_QUICK_CANDS:
        keep = cands[:MAX_QUICK_CANDS]
        if current_ip and current_ip not in keep:
            keep[-1] = current_ip
        cands = list(dict.fromkeys(keep))
        print(f"  候选 {n_before} 个 > 上限, 按粗筛分取前 {len(cands)} 个快测"
              f"(丢 {n_before - len(cands)} 个粗筛垫底, 省约 {(n_before - len(cands)) * QUICK_ROUNDS * QUICK_MAX_SEC:.0f}s)",
              flush=True)
    print(f"  候选 {len(cands)} 个 (Top{SCREEN_N}={len(top)} + 本地保送={len(gd)} + 当前={1 if current_ip else 0})", flush=True)
    quick = {}
    t_q = time.perf_counter()
    for ip in cands:
        left = QUICK_BUDGET_SEC - (time.perf_counter() - t_q)
        if quick and left <= 0:
            print(f"  快测预算 {QUICK_BUDGET_SEC:.0f}s 用尽, 余 {len(cands) - len(quick)} 个候选跳过(用已测最优)", flush=True)
            break
        quick[ip] = median_quick(ip, host, url_path)
    # 预算提前 break 时 quick 里没有剩余候选的键, 必须按 quick 取而不是按 cands(否则 KeyError)
    results = sorted(((sp_, ip_) for ip_, sp_ in quick.items() if sp_ > 0), reverse=True)
    if not results:
        return None
    sp, ip = results[0]
    print(f"  -> 快测冠军 {ip} {tag_of.get(ip, '').strip()[:28]} median={sp:.1f}MiB/s", flush=True)
    return {"group": name, "ip": ip, "speed": round(sp, 1),
            "region": tag_of.get(ip, "").strip()[:40], "rows": full_rows,
            "tag_of": tag_of, "ranked": results}


def router_cmd(cmd):
    """在路由器上执行命令, 返回 stdout 或 None"""
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
                            "-i", SSH_KEY, f"root@{ROUTER}", cmd],
                           capture_output=True, text=True, timeout=30)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception as e:
        print(f"  [router] ssh 失败: {e}", flush=True)
        return None


def router_apply(domains, ip, current):
    """幂等更新: 删该域名全部历史值再写新值(防 address 堆积), 全部命令一次 ssh 调用"""
    hist = parse_hist(current)
    cmds = []
    for d in domains:
        olds = hist.get(d)
        if olds == {ip}:
            continue  # 已是最新且唯一
        cmds += [f"uci del_list dhcp.@dnsmasq[0].address='/{d}/{o}'" for o in sorted(olds or [])]
        cmds.append(f"uci add_list dhcp.@dnsmasq[0].address='/{d}/{ip}'")
    if not cmds:
        return False, "no change"
    cmds.append("uci commit dhcp")
    r = router_cmd("; ".join(cmds))
    if r is None:
        return False, "uci update failed"
    if router_cmd("/etc/init.d/dnsmasq restart") is None:
        print("  [router] dnsmasq restart 失败(uci 已提交, LAN DNS 仍是旧记录, 需人工重启复核)", flush=True)
    return True, "dnsmasq updated+restarted"


def main():
    # 运行锁: 防 cron 与手动并发(双写路由器/结果文件); ImportError(非Linux调试)≠锁占用, 分开处理
    try:
        import fcntl
    except ImportError:
        fcntl = None
    if fcntl:
        lock_fd = open(os.path.join(BASE, ".run.lock"), "w")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            print("已有实例运行, 退出", flush=True)
            return
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", action="append", choices=[p[0] for p in POOLS],
                    help="只测指定组(可多次), 缺省全测")
    only = set(ap.parse_args().pool or [])
    pools = [p for p in POOLS if not only or p[0] in only]
    t0 = time.time()
    # 环境自检: cron 环境的 HOME/密钥/路由器可达性(明早看 run.log 首行即知)
    print(f"[env] HOME={os.path.expanduser('~')} key_ok={os.path.exists(SSH_KEY)} py={sys.version.split()[0]}", flush=True)
    current = router_cmd("uci show dhcp | grep address")
    if current is None:
        print("[env] 路由器 ssh 不可达, 本轮整体跳过(测速结果无法应用, 不白烧 10 分钟带宽)", flush=True)
        return
    hist = parse_hist(current) if current else {}
    cur_ip = {}
    for name, _p, _h, _u, doms in POOLS:
        first = doms[0]
        cur_ip[name] = sorted(hist.get(first, []))[0] if hist.get(first) else None
    print(f"=== Xbox 测速 {time.strftime('%Y-%m-%d %H:%M:%S')} 当前IP: " +
          " ".join(f"{n}={v}" for n, v in cur_ip.items() if n in [p[0] for p in pools]), flush=True)
    summary = []
    for name, path, host, url_path, doms in pools:
        try:
            r = test_pool(name, path, host, url_path, cur_ip[name])
        except Exception as e:
            print(f"  [err] {name}: {e}", flush=True)
            continue
        if not r:
            print(f"  [warn] {name}: 无有效结果, 跳过", flush=True)
            continue
        cand = r["ip"]
        results = r.pop("ranked", [])
        r.pop("rows", None)        # 全池明细不落盘(看门狗只用 hotspares, 白占体积)
        tags = r.pop("tag_of", {})   # 热备分散要用运营商标签, 但不落盘
        if cand == cur_ip[name]:
            r["router"] = "no change (same ip)"
            # 冠军自己的稳态必须测: 只存快测中位数会让看门狗拿它当劣化基准(快测虚高 -> 劣化误判),
            # 且 hotspares[0]=冠军 是 load_record 的取值前提
            st_self = median_steady(cand, host, url_path) if cand else 0.0
            head = [{"ip": cand, "steady": round(st_self, 1)}] if st_self > 0 else []
            if head:
                r["speed"] = head[0]["steady"]
            r["hotspares"] = (head + steady_top_diverse(results, {cand}, host, url_path, tags,
                                                        HOTSPARE_N - len(head),
                                                        {op_of(tags.get(cand, ""))}))[:HOTSPARE_N]
            summary.append(r)
            print(f"  [router] no change (same ip {cand}, steady {st_self:.1f})", flush=True)
            continue
        # 稳态复测: 冠军候选 vs 当前IP, 赢迟滞阈值才换(摊平突发, 防虚高换节点)
        st_cand = median_steady(cand, host, url_path)
        st_cur = median_steady(cur_ip[name], host, url_path) if cur_ip[name] else 0.0
        wr = WIN_RATIO.get(name, 1.15)
        if st_cur > 0 and st_cand < st_cur * wr:
            r["router"] = f"kept (steady {st_cand:.1f} < cur {st_cur:.1f}*{wr})"
            r["ip"] = cur_ip[name]
            r["speed"] = round(st_cur, 1)
            r["hotspares"] = ([{"ip": cur_ip[name], "steady": round(st_cur, 1)}] +
                              steady_top_diverse(results, {cur_ip[name]}, host, url_path, tags,
                                                 HOTSPARE_N - 1, {op_of(tags.get(cur_ip[name], ""))}))
            print(f"  [router] 保持 {cur_ip[name]}: 冠军稳态 {st_cand:.1f} < 当前稳态 {st_cur:.1f}*{wr}", flush=True)
        elif st_cur == 0 and st_cand > 0:
            # 当前IP测不出但冠军稳态有效(st_cand 本身即验证) => 当前IP已死, 必须切换;
            # 迟滞阈值只适用于两个都健康时的防抖, 对死IP不适用
            changed, detail = router_apply(doms, cand, current)
            r["speed"] = round(st_cand, 1)
            r["router"] = detail + " (cur dead/unmeasurable, switched)"
            r["hotspares"] = ([{"ip": cand, "steady": round(st_cand, 1)}] +
                              steady_top_diverse(results, {cand}, host, url_path, tags,
                                                 HOTSPARE_N - 1, {op_of(tags.get(cand, ""))}))
            print(f"  [router] 当前IP稳态测不出, 切换 {cand} (稳态 {st_cand:.1f})", flush=True)
        elif st_cur == 0:
            # 冠军稳态也测不出: 无已验证的替换者, 保守保持
            r["router"] = "kept (cur & cand unmeasurable)"
            r["ip"] = cur_ip[name]
            r["speed"] = 0.0
            r["hotspares"] = steady_top_diverse(results, {cur_ip[name]}, host, url_path, tags,
                                                HOTSPARE_N)
            print(f"  [router] 保持 {cur_ip[name]}: 当前IP与冠军稳态均测不出", flush=True)
        else:
            changed, detail = router_apply(doms, cand, current)
            r["speed"] = round(st_cand, 1)
            r["router"] = detail + (" 需Windows复核" if name == "Cn2" and changed else "")
            r["hotspares"] = ([{"ip": cand, "steady": round(st_cand, 1)}] +
                              steady_top_diverse(results, {cand}, host, url_path, tags,
                                                 HOTSPARE_N - 1, {op_of(tags.get(cand, ""))}))
            print(f"  [router] {detail}", flush=True)
        summary.append(r)
    elapsed = int(time.time() - t0)
    # 按组合并写入(--pool 定向重测只测一组, 不得覆盖另一组的热备名单) + 原子写(防看门狗读到半截)
    out_path = os.path.join(BASE, "results.json")
    try:
        existing = json.load(open(out_path, encoding="utf-8"))
        existing = existing if isinstance(existing, list) else []
    except Exception:
        existing = []
    by_group = {g.get("group"): g for g in existing if isinstance(g, dict)}
    for g in summary:
        by_group[g["group"]] = g
    merged = [by_group[p[0]] for p in POOLS if p[0] in by_group]
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    os.replace(tmp, out_path)
    log_line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')} {elapsed}s " +
                " | ".join(f"{g['group']}={g['ip']}({g['speed']}MiB/s,{g['router']})" for g in summary))
    with open(os.path.join(BASE, "speed.log"), "a", encoding="utf-8") as f:
        f.write(log_line + "\n")
    print(log_line, flush=True)


if __name__ == "__main__":
    main()
