#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Xbox 加速看门狗 v2.3.4: 每小时体检两组钉住 IP (TCP 连通 + 8 秒吞吐),
任一组死掉、吞吐 < 8MiB/s、或掉到记录值一半以下(degraded) =>
  1) 热备止血: 读 results.json 的 hotspares(NAS 上轮全量测速验证过的次优节点),
     逐个用热备自己的测速对实测 8s, 第一个达标的**整组域名一起** uci 切过去 (分钟级恢复, 不等全量重测)
     达标线: 死/极慢 >= 20MiB/s; 只是劣化则还要比当前实测再快 10%(否则越止越慢)
  2) 定向重测: 触发 xbox-speedtest.py --pool <坏组> (只重测坏组, 不再全池 3.2h)
v2.3.4 (2026-11-06): 交付流程整改 —— 引入 tests/audit.py 机器审计, 本脚本逻辑未改
v2.3.3 (2026-11-06 交付审计):
  - 修 bug: 止血只切首域名(assets1), assets2/d1/d2 仍指向死节点, 主机命中那几个域名照样龟速,
    日志却报"止血成功" -> 改为整组域名一起切(targets_for), 并一并扫掉仍指向死 IP 的其他钉住域名
  - 修 bug: dnsmasq restart 失败时 router_apply 仍返回 True -> 改为返回 False, 视作止血失败,
    让下一个热备/定向重测接手, 不再谎报成功
  - 改进: 单次 8s 采样抖动就判死/劣化会误切(实测 108 的节点偶发掉到 40) -> 非 ok 时复测一次取高值,
    两次都低才动手
v2.3.2: 加相对劣化判定(DEGRADE_RATIO)。2026-11-06 实测 Cn1 从 108 -> 43.8(18:30) -> 10.6(19:30) -> 0.8(20:30),
        绝对阈值 8MiB/s 让用户半速跑了两小时才等到切换; 相对判定提前到掉半就动手
v2.2: 止血验证改用热备所属组的测速对(Cn2 热备用 dlassets+版本化URL, 防 assets1 虚快误切)
已知局限: 探测不到"只掐游戏内容不掐测速文件"的 CDN 配额限速(需令牌),
该类症状(主机脉冲式卡顿)靠人工切换冷节点兜底。
用法: python3 watchdog.py ; cron: 30 * * * * (避开 04:00 全量测速窗口)
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
import http.client

BASE = os.path.dirname(os.path.abspath(__file__))
SSH_KEY = os.path.expanduser("~/.ssh/id_ed25519_router")
ROUTER = os.environ.get("XBOX_ROUTER_IP", "192.168.1.1")  # 路由器内网 IP:环境变量 XBOX_ROUTER_IP 覆盖,默认仅通用示例
THRESHOLD_MIBS = 8.0
# 相对劣化: 绝对阈值封不住"慢慢烂掉"——2026-11-06 实测 Cn1 从 108 一路 43.8(18:30)/10.6(19:30)/0.8(20:30),
# 前两小时都判 ok, 用户实际半速跑了两小时才等到切换。低于记录值一半就提前动手。
DEGRADE_RATIO = 0.55           # 实测 < 记录稳态 * 0.55 判 degraded
DEGRADE_MIN_RECORD = 25.0      # 记录值本身不到这个数不做相对判定(本就不快的节点别抖)
HOTSPARE_MIN_MIBS = 20.0      # 止血门槛: 热备实测 >= 此值才切(且必须显著高于 THRESHOLD)
DL_TIMEOUT = 12.0
STEADY = 8.0
TEST_HOST = "assets1.xboxlive.cn"
TEST_PATH = "/Z/routing/extraextralarge.txt"

POOLS = [  # 注意: 3 元组, 与 xbox-speedtest.py 的 POOLS(5 元组)结构不同; 解包请用 DOMAIN/TESTPATH 字典, 勿直接 for a,b in POOLS
    ("Cn1", "assets1.xboxlive.cn",
     "/Z/routing/extraextralarge.txt"),
    ("Cn2", "dlassets.xboxlive.cn",
     "/public/content/1b5a4a08-06f0-49d6-b25f-d7322c11f3c8/372e2966-b158-4488-8bc8-15ef23db1379/1.5.0.1018.88cd7a5d-f56a-40c7-afd8-85cd4940b891/ACUEU771E1BF7_1.5.0.1018_x64__b6krnev7r9sf8"),
]
DOMAIN = dict((p[0], p[1]) for p in POOLS)
TESTPATH = dict((p[0], p[2]) for p in POOLS)
# 每组要一起切的**全部**钉住域名, 必须与 xbox-speedtest.py POOLS 的第 5 项一致。
# 2026-11-06 审计发现: 止血只切首域名(assets1)时, assets2/d1/d2 仍指向死节点,
# Xbox 命中那几个域名照样龟速 —— 日志会说"止血成功"而实际只切了四分之一。
DOMAINS = {
    "Cn1": ("assets1.xboxlive.cn", "assets2.xboxlive.cn", "d1.xboxlive.cn", "d2.xboxlive.cn"),
    "Cn2": ("dlassets.xboxlive.cn", "dlassets2.xboxlive.cn"),
}
# 注1: mp 下载域名已改原生 DNS 解析(不钉 IP),不在看门狗管辖内
# 注2: 健康体检统一用 Cn1 测速文件(assets1 域名), 不依赖 Cn2 版本化 URL(微软下架版本会误报整组死亡);
#      Cn2 的权威测量在 speedtest 侧, 这里只做"活着没有/够不够快"的哨兵
# 注3: 但热备止血验证必须用热备自己的测速对(Cn2 热备是 dlassets 边缘, speedtest 用 dlassets+版本化URL
#      实测入围)——拿 assets1 内容去验会误切"assets1 快但 dlassets 慢"的节点


class BindIPConn(http.client.HTTPConnection):
    def __init__(self, host, ip, timeout):
        super().__init__(host, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, 80), timeout=self.timeout)


def ts():
    return time.strftime("%F %T")


def router_cmd(cmd):
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
                            "-i", SSH_KEY, f"root@{ROUTER}", cmd],
                           capture_output=True, text=True, timeout=30)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception as e:
        print(f"[router] ssh 失败: {e}", flush=True)
        return None


def parse_hist(current):
    hist = {}
    for m in re.finditer(r"'/([^/]+)/([0-9.]+)'", current):
        hist.setdefault(m.group(1), set()).add(m.group(2))
    return hist


def get_pins():
    """从路由器 uci 读两组首域名的钉住 IP"""
    current = router_cmd("uci show dhcp | grep address")
    pins = {}
    if current:
        hist = parse_hist(current)
        for name, domain in DOMAIN.items():
            ips = hist.get(domain)
            pins[name] = sorted(ips)[0] if ips else None
    return pins


def targets_for(name, old_ip, hist):
    """本组要一起切的域名: 显式表(DOMAINS) + 当前仍指向死 IP 的其他钉住域名。
    后者防配置漂移: 若 speedtest 哪天多钉了域名, 这里也能一起切走, 不留半死状态。
    old_ip 为 None(无钉住)时只用显式表。"""
    doms = list(DOMAINS.get(name) or (DOMAIN[name],))
    if old_ip:
        for d, ips in (hist or {}).items():
            if d not in doms and ips == {old_ip}:
                doms.append(d)
    return doms


def router_apply(domains, ip):
    """幂等更新一组域名: 删各自的全部历史值再写新值(防 address 堆积), 全部命令一次 ssh 调用。
    返回 True 才算真的生效(commit + dnsmasq restart 都成功); 只 commit 不 restart 时
    路由器仍返回旧记录, 视作失败, 交由下一个热备/定向重测处理。"""
    current = router_cmd("uci show dhcp | grep address")
    hist = parse_hist(current or "")
    cmds = []
    for d in domains:
        olds = hist.get(d, set())
        if olds == {ip}:
            continue
        cmds += [f"uci del_list dhcp.@dnsmasq[0].address='/{d}/{o}'" for o in sorted(olds)]
        cmds.append(f"uci add_list dhcp.@dnsmasq[0].address='/{d}/{ip}'")
    if not cmds:
        return True                      # 全部已是最新且唯一
    cmds.append("uci commit dhcp")
    if router_cmd("; ".join(cmds)) is None:
        return False
    if router_cmd("/etc/init.d/dnsmasq restart") is None:
        print(f"{ts()} [router] dnsmasq restart 失败(uci 已提交但 DNS 未生效, 需人工复核)", flush=True)
        return False
    return True


def steady(ip, host=TEST_HOST, path=TEST_PATH):
    """绑定 IP 下载 8 秒, 返回 MiB/s (0=失败)"""
    try:
        conn = BindIPConn(host, ip, DL_TIMEOUT)
        conn.request("GET", path, headers={"Host": host, "Range": "bytes=0-1073741823",
                                           "User-Agent": "XboxDownload", "Accept-Encoding": "identity"})
        resp = conn.getresponse()
        if resp.status not in (200, 206):
            resp.close()
            return 0.0
        t0 = time.perf_counter()
        got = 0
        while time.perf_counter() - t0 < STEADY:
            b = resp.read(65536)
            if not b:
                break
            got += len(b)
        el = time.perf_counter() - t0
        resp.close()
        return got / (1048576 * max(el, 0.1))
    except Exception:
        return 0.0


def load_hotspares(name):
    """读上轮 speedtest 写入的热备名单 [{ip, steady}, ...]"""
    try:
        data = json.load(open(os.path.join(BASE, "results.json"), encoding="utf-8"))
    except Exception:
        return []
    for grp in data:
        if grp.get("group") == name:
            return grp.get("hotspares", []) or []
    return []


def load_record(name):
    """读上轮记录的冠军稳态(优先 hotspares[0].steady, 其次 speed) —— 相对劣化的基准"""
    try:
        data = json.load(open(os.path.join(BASE, "results.json"), encoding="utf-8"))
    except Exception:
        return 0.0
    for grp in data:
        if grp.get("group") != name:
            continue
        hs = grp.get("hotspares") or []
        if hs and hs[0].get("ip") == grp.get("ip") and hs[0].get("steady"):
            try:
                return float(hs[0]["steady"])
            except (TypeError, ValueError):
                pass
        try:
            return float(grp.get("speed") or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def quick_fix(name, pinned_ip, floor_mibs=HOTSPARE_MIN_MIBS):
    """热备止血: hotspares 逐个实测, 第一个达标的整组切过去, 返回切到的 IP 或 None。
    floor_mibs: 达标线。degraded 场景要传"当前实测×1.1", 否则会把 44MiB/s 的节点换成 30MiB/s 的(越止越慢)"""
    doms = targets_for(name, pinned_ip, parse_hist(router_cmd("uci show dhcp | grep address") or ""))
    for hs in load_hotspares(name):
        ip = hs.get("ip")
        if not ip or ip == pinned_ip:
            continue
        sp = steady(ip, host=DOMAIN[name], path=TESTPATH[name])
        print(f"{ts()} [{name}] 热备 {ip} (记录 {hs.get('steady')}MiB/s) 实测 {sp:.1f}MiB/s", flush=True)
        if sp >= floor_mibs:
            if router_apply(doms, ip):
                print(f"{ts()} [{name}] 止血成功 => 切 {ip} ({len(doms)} 个域名: {' '.join(doms)})", flush=True)
                return ip
            else:
                print(f"{ts()} [{name}] 热备 {ip} 达标但 uci 写入/重启失败, 试下一个", flush=True)
    return None


def classify(sp, rec):
    """返回 (kind, 打印用label)。kind: dead / slow / degraded / ok"""
    if sp == 0:
        return "dead", "dead"
    if sp < THRESHOLD_MIBS:
        return "slow", f"slow({sp:.1f}<{THRESHOLD_MIBS})"
    if rec >= DEGRADE_MIN_RECORD and sp < rec * DEGRADE_RATIO:
        return "degraded", f"degraded({sp:.1f} < 记录 {rec:.1f}*{DEGRADE_RATIO})"
    return "ok", "ok"


def main():
    # 运行锁: ImportError(非Linux调试)≠锁占用, 分开处理
    try:
        import fcntl
    except ImportError:
        fcntl = None
    if fcntl:
        lock_fd = open(os.path.join(BASE, ".watchdog.lock"), "w")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            print("已有看门狗实例, 退出", flush=True)
            return
    pins = get_pins()
    if not any(pins.values()):
        print(f"{ts()} [skip] 路由器不可达或无钉住记录", flush=True)
        return
    bad, spd = [], {}
    for name in DOMAIN:
        ip = pins.get(name)
        if not ip:
            bad.append(name)
            print(f"{ts()} [{name}] 无钉住 IP => 异常", flush=True)
            continue
        sp = steady(ip)
        rec = load_record(name)
        kind, label = classify(sp, rec)
        if kind != "ok":
            # 单次 8s 采样在晚高峰会抖(实测 108 的节点偶发掉到 40): 复测一次取高值,
            # 两次都低才动手 —— 否则会把好节点换成更慢的热备, 典型的"越止越慢"
            sp2 = steady(ip)
            if sp2 > sp:
                print(f"{ts()} [{name}] 复测 {sp2:.1f}MiB/s (首测 {sp:.1f}MiB/s), 判据取高值", flush=True)
                sp = sp2
                kind, label = classify(sp, rec)
        spd[name] = sp
        print(f"{ts()} [{name}] {ip} steady={sp:.1f}MiB/s => {label}", flush=True)
        if kind != "ok":
            bad.append(name)
    if bad:
        # 第一步: 热备止血(分钟级), 有钉住 IP 的坏组才试
        for name in bad:
            if pins.get(name):
                # 达标线: 死/极慢用固定门槛; 只是劣化则必须比当前实测再快 10%, 否则越止越慢
                floor = HOTSPARE_MIN_MIBS
                if spd.get(name, 0) >= THRESHOLD_MIBS:
                    floor = max(HOTSPARE_MIN_MIBS, spd[name] * 1.1)
                quick_fix(name, pins[name], floor)
        # 第二步: 定向重测坏组(speedtest 会读到止血后的新钉住 IP, 迟滞保护不会立刻换走)
        groups = list(dict.fromkeys(bad))
        args = []
        for g in groups:
            args += ["--pool", g]
        print(f"{ts()} [trigger] 异常组 {groups} => 触发定向重测 {' '.join(args)}", flush=True)
        subprocess.run([sys.executable or "python3", os.path.join(BASE, "xbox-speedtest.py")] + args, cwd=BASE)
    else:
        print(f"{ts()} [all-ok] 各组健康, 无动作", flush=True)


if __name__ == "__main__":
    main()
