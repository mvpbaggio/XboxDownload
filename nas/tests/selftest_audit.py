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
  M7 假分散        op_of 退回"直接取末段"(域名冒充运营商)            -> C25
  M8 稳态退化      dl_steady 恢复"一个 Range 下完就退"                 -> C28
  M9 日志假账      候选构成去掉"截断前"口径(8+31+1≠16)                -> A13
  M10 主程序谎报    xbox-speedtest.router_apply restart 失败仍成功      -> C29
  M11 续测重下头部  _req 的 Range 起点写死 0(268MB 只覆盖 160MiB)      -> C30
  M12 口径不一致    watchdog 的 STEADY_MAX_BYTES 与 speedtest 不同     -> A15
  M13 当前IP取错    parse_hist 退回 set(丢写入顺序)                    -> C32
  M14 计时含建连    dl_steady 的 t0 回到循环之前(把 TCP 建连算进吞吐)   -> A17
  M15 只修一处      watchdog 的 parse_hist 退回 set(主程序那份没回退)   -> C32b
  M16 去掉替换下限  REPLACE_MIN 20->0(14 的冠军也敢换掉 93 的节点)     -> C33
  M17 跨度超容忍值  RANGE_SPAN 160MiB->256MiB(实测该值直接超时)         -> C30b
  M18 基准排到队尾  快测回到 for ip in cands(预算一超先跳过当前 IP)      -> C34
  M19 基准取值来源  q_cur 回到从 results 取(被 sp>0 过滤过的表)          -> C34b
  M20 咬回默认参数  watchdog.steady 恢复 host/path 默认值(Cn1 的文件)   -> C35
  M21 静默测错文件  main 里改回裸调用 steady(ip)(Cn2 测到 Cn1 文件)     -> C35b
  M22 快测顶账      同 IP 稳态 0 时不再置 speed=0(留下快测虚高值)        -> C36
  M23 抖动不外露    稳态极差超 25% 也不再打离散度(只剩一个中位数)        -> C37
  M24 冠军被测两次  保持分支去掉 known=(重测值覆盖决策值/名单静默缺人)   -> C38b
  M25 流量日志假账  打 STEADY_CAND_MAX 候选数(乘法乘不平)               -> C39b
  M26 节点计数不涨  _STEADY_NODES 不再自增(日志永远 0 个节点)           -> C39
  M27 日志打假不等式 离散度日志退回"极差 22.8 > 25%"(绝对值比比例)      -> C40
  M28 稳健性判反    最高值不到阈值也报"受抖动支配"(把警告当结论)        -> C41
  M29 算了不调用    decision_note 写好但 main 不调用(等于没加)          -> C41c
  M30 零值不报      0/0/100 只当中位 100.0, 不说几轮为 0(看起来像三轮都成)  -> C42
  M31 出数轮不报    判定措辞里"仅 1/3 轮出数"被删掉(结论像基于三轮)        -> C42b
  M32 快测顶账      稳态测不出时把 quick 值写进 speed(只改 alive 那条分支)  -> C43
  M33 门槛漂移      三份 20MiB/s 门槛只改一份(注释要求一致, 无人检查)       -> C44
  M34 已测值漏冠军  st_cur==0 分支重测冠军(名单留第二次抖动值)             -> C45
  M35 已测值漏旧节点 切换分支重测刚换下来的节点(唯一备胎从名单消失)         -> C45b
  M36 清单版本漂移   README 文件清单写回旧版本(A6 只查全文包含, 照样绿)   -> C46
  M37 冷却失效       拿到冷却值又乘 0(等于没加冷却)                       -> C48
  M38 未来时间戳不设防 标记时间在未来时不再按"没见过"处理(重测被冻死)      -> C48d
  M39 双写者         watchdog 也去写冷却标记(两边口径迟早不同)             -> C49
  M40 主程序不打标记  mark_retest 变空函数(看门狗退化成无退避)             -> C49b
  M41 退回 53 解析    pool_refresh 用回 getaddrinfo(永远 appended 0)       -> C47
  M42 日志丢 resolved appended 0 分不清"池子最新"与"一个都没解析到"        -> C47b
  M43 冷却不够长      3 小时退回 45 分钟(< cron 周期, 冷却一次都不命中)     -> C50
  M44 只在开头打标记  去掉收尾那次(长跑把冷却窗口自己吃掉)                 -> C51
  M45 同 IP 不落盘    同 IP 分支不再调 router_apply("切了一半"永远不修)     -> C52
  M46 各写各的锁      watchdog 用自己的锁名(测速期间照样改路由器)          -> C53
  M47 失败说成没变化   同 IP 分支把 apply 失败记成 no change(新代码里的谎报)   -> C54
  M48 锁建不了=在跑    run_lock_taken 的 open 失败仍返回 False(永久跳止血)    -> C55
  M49 止血不记账      quick_fix 成功后不回写 results.json(记录说死 IP)        -> C56c
  M50 跨组重复不告警   热备恰是另一组钉住的 IP 时静默(独立性是假的)           -> C57
  M51 基准不跟着走     record_pin 只写 grp.speed(实测值到不了 load_record)      -> C58
  M52 标记早于检查     mark_retest 挪回路由器可达之前(早退轮白占 3h 冷却)       -> C60
  M53 体检口径回潮     注2 写回"体检统一用 Cn1 文件"(爆炸半径说小)            -> C59
  M54 grep 空匹配当不可达 (去掉 || true, 干净路由器上 rc=1 被折成 None, 整轮跳过)  -> C61b
  M55 触发子进程无上限 (去掉 timeout, 卡死时把 .watchdog.lock 一起堵到下一小时)  -> C62
  M56 watchdog 少一个 || true (只回退 get_pins 那一处; 桩里 router_cmd 被替换, C64 行为断言仍绿, 只有 C64b 命令串能抓)  -> C64b
  M57 get_pins 三态压平 (ssh 不可达也返回 {}, 与'可达但空配置'再也分不开)  -> C64
  M58 劣化基准静默 (results.json 读不出时不打日志, degraded 档悄悄失效)  -> C65
  M59 台账长歪 (把最新一条挪到文件底部: 头部说'新条目加在最上面', 文件实际不是严格倒序)  -> A18
  M60 引用假账 (台账头部把一个不存在的检查编号写成'由它钉住': 行为一点不变, 只是下一个读的人会去找一条没有的检查)  -> A19
  M61 扫描边界丢失 (README 的「## 版本历史」标题被改名: A19 静默退回扫全文, 历史引用又变成永久 API)  -> A20
  M62 变异清单停在旧版本 (自检头部清单里删掉 M1 这一行: 自检照样全过, 但下一个读的人看不到这条)  -> A21
  M63 历史区被误扫 (往 README 版本历史里塞两个不存在的编号 C99/B77: 那是记账, A19 必须仍然绿)  -> GREEN
  M64 条目格式漂移 (最新一条缩进两格 + 挪到文件底部: 顶格才被解析, 缩进就整条消失)  -> A18
  M65 真话被判成假账 (台账头部写'任何一段崩了由 C0 兜住': A0/B0/C0/D0 是动态传的, 真实存在)  -> GREEN
  M66 台账历史被砍 (只留最新 20 条: 头部最新、底下删空, 读起来和真的没区别)  -> A18
  M67 py3.8 API 漂移 (pool_refresh.py 里写 .removeprefix: 语法合法、3.8 运行时 AttributeError)  -> A22
  M68 py3.8 语法漂移 (pool_refresh.py 里写 match 语句: 3.10+ 才有, ast feature_version 必须拦下)  -> A22
  M69 条数灌水 (砍掉 10 条顶层 + 底部补 10 条更旧的嵌套日期子项: 日期行总数不变, 只有真的数顶层才能抓)  -> A18
  M70 日期子项假红 (最新条目下插一条带日期的嵌套子项: 它不是条目, 病名必须是"日期子项"而不是"逆序")  -> A18
  M71 注解泛型下标 (pool_refresh.py 写 def f(x: list[int]): 3.8 语法合法、定义时求值就 TypeError)  -> A22
  M72 池子写入不校验 (丢掉 ip_reject_reason 的结果: 0.0.0.0/环回/私网/CGNAT 全写进池)  -> C66
  M73 粗筛无总预算 (预算判定恒不成立 = 退回 ex.map 全量跑完, 30 个节点最坏 296s)  -> C67
  M74 粗筛预算没有保底 (去掉 SCREEN_MIN_DONE: 预算秒空 => 候选表空 => 整组判无冠军)  -> C67b
  M75 RTT 取最后一次 (min-of-N 退化成"留最后一次": 一次 1020ms 抖动就把 89MiB/s 的节点判成远)  -> C68
  M76 L0 无总预算 (预算判定恒不成立: 261 个节点 × 3 次握手, 池子越长越无边)  -> C69
  M77 握手失败当 0ms (ms 非 None 那道门压成 0.0: 死节点升序排第一, 挤掉真节点)  -> C68

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
# A18(台账倒序)是跨目录断言: 台账在项目上一级, 不在 FILES 里。自检必须显式指路,
# 否则 audit.py 在 tmp 里找不到它、只能判红 —— 那就成了"自检本身失效"。
LEDGER = os.path.join(os.path.dirname(SRC), "AI共管台账.md")

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
    ("M7", "热备假分散 (op_of 退回直接取末段)", "xbox-speedtest.py", "C25",
     '    last = t.split()[-1]\n    if _NOT_OP_RE.match(last.lower()):\n        return ""\n    return last',
     '    return t.split()[-1]'),
    ("M8", "稳态退化 (读完第一个 Range 就退, 回到只测开头 1.5 秒)", "xbox-speedtest.py", "C28",
     "            if got == n0:\n                break  # 这一轮一个字节都没拿到, 不要重连自旋",
     "            break"),
    ("M9", "日志假账 (候选构成混用截断前后计数)", "xbox-speedtest.py", "A13",
     '个 (截断前: Top{SCREEN_N}', '个 (Top{SCREEN_N}'),
    ("M10", "主程序谎报成功 (restart 失败仍返回 True)", "xbox-speedtest.py", "C29",
     '        return False, "uci committed but dnsmasq restart FAILED"\n', ''),
    ("M11", "续测从头重下 (Range 起点写死 0)", "xbox-speedtest.py", "C30",
     "_req(conn, host, path, start=got)   # 续测从 got 继续", "_req(conn, host, path)   # 续测从 got 继续"),
    ("M12", "口径漂移 (watchdog 稳态字节上限与 speedtest 不同)", "watchdog.py", "A15",
     "STEADY_MAX_BYTES = 256 * 1024 * 1024", "STEADY_MAX_BYTES = 512 * 1024 * 1024"),
    ("M13", "当前 IP 取最旧的那个 (parse_hist 变成倒序插入)", "xbox-speedtest.py", "C32",
     "            lst.append(m.group(2))", "            lst.insert(0, m.group(2))"),
    ("M14", "计时含建连 (dl_steady 的 t0 回到循环之前)", "xbox-speedtest.py", "A17",
     "        t0 = None      # 计时起点", "        t0 = 0.0       # 计时起点"),
    ("M15", "只修一处 (watchdog 的 parse_hist 退回 set)", "watchdog.py", "C32b",
     '        lst = hist.setdefault(m.group(1), [])\n        if m.group(2) not in lst:\n            lst.append(m.group(2))',
     '        hist.setdefault(m.group(1), set()).add(m.group(2))'),
    ("M16", "去掉替换下限 (REPLACE_MIN 20->0, 14 的冠军也照切)", "xbox-speedtest.py", "C33",
     "REPLACE_MIN = 20.0", "REPLACE_MIN = 0.0"),
    ("M17", "Range 跨度超 CDN 容忍值 (160MiB->256MiB, 真机该值直接超时)", "xbox-speedtest.py", "C30b",
     "RANGE_SPAN = 160 * 1024 * 1024", "RANGE_SPAN = 256 * 1024 * 1024"),
    ("M18", "迟滞基准排到队尾 (快测回到 for ip in cands)", "xbox-speedtest.py", "C34",
     "    for ip in order:", "    for ip in cands:"),
    ("M19", "基准取值来源退化 (q_cur 从被过滤的 results 取)", "xbox-speedtest.py", "C34b",
     "q_cur = quick.get(cur_ip[name], 0.0) if cur_ip[name] else 0.0",
     "q_cur = next((sp for sp, ip in results if ip == cur_ip[name]), 0.0) if cur_ip[name] else 0.0"),
    ("M20", "咬回默认参数 (watchdog.steady 又默认测 Cn1 的文件; v2.3.19 删了 TEST_HOST/TEST_PATH 常量, 这里直接注字面量)", "watchdog.py", "C35",
     "def steady(ip, host, path):",
     "def steady(ip, host='assets1.xboxlive.cn', path='/Z/routing/extraextralarge.txt'):"),
    ("M21", "静默测错文件 (main 里改回裸调用 steady(ip))", "watchdog.py", "C35b",
     "        sp = steady(ip, host=DOMAIN[name], path=TESTPATH[name])", "        sp = steady(ip)"),
    ("M22", "快测顶账 (同 IP 稳态 0 时不再把 speed 置 0)", "xbox-speedtest.py", "C36",
     '                r["speed"] = 0.0\n                r["router"] = "no change (same ip, steady unmeasurable)"',
     '                r["router"] = "no change (same ip)"'),
    ("M23", "抖动不外露 (稳态极差超 25% 也不再打离散度)", "xbox-speedtest.py", "C37",
     "    if med > 0 and (hi - lo) > med * 0.25:", "    if False:"),
    ("M24", "冠军被测两次 (保持分支去掉 known=, 重测覆盖决策值)", "xbox-speedtest.py", "C38b",
     "                                                 known=known_measured))\n"
     "            print(f\"  [router] 保持 {cur_ip[name]}: 冠军稳态",
     "                                                 ))\n"
     "            print(f\"  [router] 保持 {cur_ip[name]}: 冠军稳态"),
    ("M25", "流量日志打候选数 (乘法乘不平, 4 候选=3GiB 而真机 4.02GiB)", "xbox-speedtest.py", "C39b",
     "实测节点 {_STEADY_NODES[0]} 个 × ", "{STEADY_CAND_MAX} 候选 × "),
    ("M26", "节点计数不涨 (流量日志永远打 0 个节点)", "xbox-speedtest.py", "C39",
     "    _STEADY_NODES[0] += 1\n", ""),
    ("M27", "日志打假不等式 (极差 22.8 > 25%: 绝对值和比例直接比)", "xbox-speedtest.py", "C40",
     "              f\" (极差 {hi - lo:.1f} > 中位×25%={med * 0.25:.1f}, 本轮判定受抖动影响)\", flush=True)",
     "              f\" (极差 {hi - lo:.1f} > 25%, 本轮判定受抖动影响)\", flush=True)"),
    ("M28", "稳健性判反 (最高值不到阈值也报受抖动支配)", "xbox-speedtest.py", "C41",
     "    if hi < thr:\n", "    if False:\n"),
    ("M29", "算了不调用 (decision_note 写好但 main 不打)", "xbox-speedtest.py", "C41c",
     "        _note = decision_note(cand, st_cur * wr) if st_cur > 0 else \"\"",
     "        _note = \"\""),
    ("M30", "零值轮次不报 (0/0/100 只当中位 100.0, 不说几轮为 0)", "xbox-speedtest.py", "C42",
     "    if zeros:\n", "    if False:\n"),
    ("M31", "出数轮数不报 (判定措辞把 1/3 轮出数说成三轮都成)", "xbox-speedtest.py", "C42b",
     "    tag = \"\" if len(good) == len(rs) else f\"仅 {len(good)}/{len(rs)} 轮出数, \"",
     "    tag = \"\""),
    # 快测值顶账: 只改 alive 那一条分支(同一条件在 same-ip 分支已修), 看审计能不能抓出"两处两种记账"
    # 锚点必须带上它前面那行注释: r["speed"] = 0.0 全文件有 4 处, 裸替换会把别的分支一起改坏(C0 崩)
    ("M32", "快测顶账 (稳态测不出时把 quick 值写进 speed)", "xbox-speedtest.py", "C43",
     "                # 快测值本身没丢: 它就在 router 字符串里(quick 93.8 alive), 人看得见, 但不当基准。\n"
     "                r[\"speed\"] = 0.0\n",
     "                r[\"speed\"] = round(q_cur, 1)\n"),
    # 门槛漂移: 只改三份拷贝里的一份。代码注释说"两处必须一致", 但从未有断言检查
    ("M33", "门槛漂移 (REPLACE_MIN 20->10, 另两份不动)", "xbox-speedtest.py", "C44",
     "REPLACE_MIN = 20.0    ", "REPLACE_MIN = 10.0    "),
    ("M34", "已测值漏冠军 (st_cur==0 分支重测冠军, 名单留第二次抖动值)", "xbox-speedtest.py", "C45",
     "        if st_cand > 0:\n            known_measured[cand] = st_cand\n", ""),
    ("M35", "已测值漏当前IP (切换分支重测旧节点, 唯一备胎从名单消失)", "xbox-speedtest.py", "C45b",
     "        if cur_ip[name] and st_cur > 0:\n            known_measured[cur_ip[name]] = st_cur\n", ""),
    # 文档清单版本漂移: A6 只查「当前版本在 README 全文出现过」, 版本历史里本来就有, 于是清单写旧也绿
    ("M36", "README 文件清单版本漂移 (v2.3.18 -> v2.3.8, A6 仍绿)", "nas-README.md", "C46",
     "测速主程序（v2.3.18）", "测速主程序（v2.3.8）"),
    # v2.3.14: 两条补丁各自的失效形态。冷却与 DoH 都是"看起来在跑、实际不起作用"的类型,
    # 只能靠行为断言(标记时间 -> 是否触发)和语法事实(还有没有 getaddrinfo)分别钉住。
    ("M37", "冷却失效 (watchdog 拿到冷却值又乘 0, 等于没有冷却)", "watchdog.py", "C48",
     "        left = retest_cooldown_left()", "        left = retest_cooldown_left() * 0"),
    ("M38", "未来时间戳不设防 (时钟回拨/误写一次就能把重测冻死)", "watchdog.py", "C48d",
     "    if dt < 0:\n        return 0\n", ""),
    # 多行注入点一律用三引号直写: 用 "\n" 转义时, 生成脚本里的反斜杠被处理坏过一次(整条变异静默失效)
    ("M39", "双写者 (watchdog 也去写冷却标记, 两边口径迟早不同)", "watchdog.py", "C49",
     '''        with open(os.path.join(BASE, RETEST_MARK_NAME), encoding="utf-8") as fh:
            last = int((fh.read() or "0").strip() or 0)
''',
     '''        with open(os.path.join(BASE, RETEST_MARK_NAME), encoding="utf-8") as fh:
            last = int((fh.read() or "0").strip() or 0)
        open(os.path.join(BASE, RETEST_MARK_NAME), "w").write("0")
'''),
    ("M40", "主程序不打标记 (看门狗拿不到冷却依据, 退化成无退避)", "xbox-speedtest.py", "C49b",
     '''        with open(os.path.join(BASE, RETEST_MARK_NAME), "w", encoding="utf-8") as fh:
            fh.write("%d\\n" % int(time.time()))
''',
     '''        pass
'''),
    ("M41", "退回 53 端口解析 (getaddrinfo 只能拿到钉住的 IP, 永远 appended 0)", "pool_refresh.py", "C47",
     '''            try:
                ips = doh_a(d)
''',
     '''            try:
                ips = [i[4][0] for i in socket.getaddrinfo(d, 80, socket.AF_INET)]
'''),
    ("M42", "日志丢 resolved (appended 0 分不清'池子最新'与'一个都没解析到')", "pool_refresh.py", "C47b",
     '        print(f, "resolved", resolved, "| appended", len(new), "| rejected", len(rejected),',
     '        print(f, "appended", len(new), "| rejected", len(rejected),'),
    # v2.3.15: 上一轮那两条补丁各自的"看起来在跑其实不起作用"形态。45 分钟在小时级 cron 下一次都不命中,
    # 收尾标记缺失时一次 3405s 的长跑能把整个窗口吃掉 —— 两者都靠行为断言钉住, 不靠读注释。
    ("M43", "冷却不够长 (3 小时退回 45 分钟, 小于 cron 周期 = 冷却一次都不命中)", "watchdog.py", "C50",
     "RETEST_COOLDOWN = 3 * 3600", "RETEST_COOLDOWN = 45 * 60"),
    ("M44", "只在开头打标记 (去掉收尾那次, 57 分钟的长跑自己吃掉整个冷却窗口)", "xbox-speedtest.py", "C51",
     '    # 仍是同一个 mark_retest(单写者不变); 中途异常退出时开头那次标记留在原地, 退化成"从开始算"。\n'
     '    mark_retest()',
     '    # 仍是同一个 mark_retest(单写者不变); 中途异常退出时开头那次标记留在原地, 退化成"从开始算"。'),
    ("M45", "同 IP 不落盘 (同 IP 分支不再调 router_apply, '切了一半'永远不修)", "xbox-speedtest.py", "C52",
     "            _ch, _det = router_apply(doms, cand, current)",
     '            _ch, _det = False, "no change"'),
    # 2026-11-07 09:30 现场: 测速跑到一半, 看门狗按 degraded 把 IP 换掉, 测速结束却按旧 uci 记账
    ("M46", "各写各的锁 (watchdog 用自己的锁名, 测速期间照样改路由器)", "watchdog.py", "C53",
     'fd = open(os.path.join(BASE, ".run.lock"), "w")',
     'fd = open(os.path.join(BASE, ".watchdog.run.lock"), "w")'),
    # v2.3.15 给同 IP 分支补了 router_apply, 但只用 if _ch: —— 写失败被同一行 "no change" 盖掉
    ("M47", "失败说成没变化 (同 IP 分支把 apply 失败记成 no change)", "xbox-speedtest.py", "C54",
     'r["router"] = "same ip, residue apply FAILED: " + _det',
     'r["router"] = "no change (same ip)"'),
    ("M48", "锁建不了=测速在跑 (open 失败仍返回 False, 止血永久跳过且日志指错方向)", "watchdog.py", "C55",
     "        return LOCK_ERR",
     "        return False"),
    ("M49", "止血不记账 (quick_fix 成功后不回写 results.json, 记录一直说死 IP)", "watchdog.py", "C56c",
     "                record_pin(name, ip, sp, old_ip=pinned_ip)   # 止血即记账: 此刻仍在 .run.lock 内",
     "                pass"),
    ("M50", "跨组重复不告警 (热备恰是另一组钉住的 IP 时静默)", "watchdog.py", "C57",
     "            if ip in other_pins:",
     "            if False and ip in other_pins:"),
    # v2.3.17: 两条都是"注释声称的效果没兑现"。基准那条只能查 load_record 的返回值 ——
    # 查 grp.speed 字段会绿(值确实写进去了), 正如 C38b 只查有没有 known= 是同一类毛病。
    ("M51", "基准不跟着走 (record_pin 只写 grp.speed, 实测值到不了 load_record)", "watchdog.py", "C58",
     '''                for h in (grp.get("hotspares") or []):
                    if isinstance(h, dict) and h.get("ip") == ip:
                        h["steady"] = round(float(sp), 1)   # 基准跟着 IP 走, 否则这行白写
''',
     ""),
    ("M52", "标记早于检查 (mark_retest 挪回路由器可达之前, 早退轮白占 3 小时冷却)", "xbox-speedtest.py", "C60",
     '''    current = router_cmd("uci show dhcp | grep address || true")
''',
     '''    mark_retest()   # 早退轮也占冷却
    current = router_cmd("uci show dhcp | grep address || true")
'''),
    ("M53", "体检口径回潮 (注2 写回'体检统一用 Cn1 文件', 把爆炸半径说小)", "watchdog.py", "C59",
     "# 注2: 健康体检与止血验证**都用本组自己的测速对**(v2.3.8 起; Cn2 用 dlassets + 版本化安装包 URL)。",
     "# 注2: 健康体检统一用 Cn1 测速文件, 不依赖 Cn2 版本化 URL。"),
    # v2.3.18: 两条都是"桩里看不出来"的缝。C61 是行为断言, 但桩不真走 ssh —— 删掉 || true 它照样绿,
    # 所以补 C61b 钉命令串; 触发子进程那条则是"不打结果"和"没设上限"各自可独立回归。
    ("M54", "grep 空匹配当不可达 (去掉 || true, 干净路由器上 rc=1 被折成 None, 整轮跳过)", "xbox-speedtest.py", "C61b",
     'current = router_cmd("uci show dhcp | grep address || true")',
     'current = router_cmd("uci show dhcp | grep address")'),
    ("M55", "触发子进程无上限 (去掉 timeout, 卡死时把 .watchdog.lock 一起堵到下一小时)", "watchdog.py", "C62",
     "                                   cwd=BASE, timeout=RETEST_TIMEOUT)",
     "                                   cwd=BASE)"),
    ("M56", "watchdog 少一个 || true (只回退 get_pins 那一处; 桩里 router_cmd 被替换, C64 行为断言仍绿, 只有 C64b 命令串能抓)", "watchdog.py", "C64b",
     '    current = router_cmd("uci show dhcp | grep address || true")\n    if current is None:',
     '    current = router_cmd("uci show dhcp | grep address")\n    if current is None:'),
    ("M57", "get_pins 三态压平 (ssh 不可达也返回 {}, 与'可达但空配置'再也分不开)", "watchdog.py", "C64",
     "    if current is None:\n        return None\n",
     "    if current is None:\n        return {}\n"),
    ("M58", "劣化基准静默 (results.json 读不出时不打日志, degraded 档悄悄失效)", "watchdog.py", "C65",
     '        print(f"{ts()} [record] results.json 读不出({e.__class__.__name__}: {e}); "\n              f"本轮没有劣化基准, 只判 dead/slow, degraded 档不参与", flush=True)',
     "        pass"),
    ("M59", "台账长歪 (把最新一条挪到文件底部: 头部说'新条目加在最上面', 文件实际不是严格倒序)", "LEDGER", "A18",
     "", ""),
    ("M60", "引用假账 (台账头部把一个不存在的检查编号写成'由它钉住': 行为一点不变, 只是下一个读的人会去找一条没有的检查)", "LEDGER", "A19",
     "`xbox-speedtest/tests/audit.py` 的 **A18**", "`xbox-speedtest/tests/audit.py` 的 **C99**"),
    # v2.3.22: 三条都是"边界/清单"的账, 不是逻辑 bug。A19 改成只扫当前状态区之后,
    # 它的扫描边界("## 版本历史"标题)就成了契约 -> M61 打边界; 反过来"M63 历史区里塞假编号必须仍然绿"
    # 才是这次改动真正要钉的方向(expect 用 "GREEN": 变异后审计必须全过)。
    # 注意 old 必须带换行: README 正文里也有「## 版本历史」这几个字(引用它自己的标题),
    # 不带换行时 str.replace(...,1) 命中正文里那处, 真标题毫发无损 -> A20 照样绿, 变异退化成空操作。
    # 本轮 NAS 上就这么漏过一次(自检报 M61 漏网, 正确), 本地加完这条注释后两边都红在 A20。
    ("M61", "扫描边界丢失 (README 的「## 版本历史」标题被改名: A19 静默退回扫全文, 历史引用又变成永久 API)", "nas-README.md", "A20",
     "\n## 版本历史\n", "\n## 变更记录\n"),
    ("M62", "变异清单停在旧版本 (自检头部清单里删掉 M1 这一行: 自检照样全过, 但下一个读的人看不到这条)", "SELFTEST", "A21",
     "  M1 常量漂移", "  常量漂移"),
    ("M63", "历史区被误扫 (往 README 版本历史里塞两个不存在的编号 C99/B77: 那是记账, A19 必须仍然绿)", "nas-README.md", "GREEN",
     "- v2.3.13 第七次真机审计", "- v2.3.13 第七次真机审计（历史条目点名 C99、B77 这类编号不算假账）"),
    # v2.3.23: 三条都是"解析器自己不够硬"。F1 是同一个缝第三轮提 —— 前两轮我只加注释没动那行,
    # 因为当时以为"真台账全顶格就不会出事"; 外部审计连续三轮把六种写法摆出来, 才看清
    # `startswith("- ")` 就是它自己注释里警告的那个后门。
    ("M64", "条目格式漂移 (最新一条缩进两格 + 挪到文件底部: 顶格才被解析, 缩进就整条消失)", "LEDGER", "A18",
     "", ""),
    ("M65", "真话被判成假账 (往台账头部写'任何一段崩了由 C0 兜住': A0/B0/C0/D0 是 chk(cid,...) 动态传的, 真实存在)", "LEDGER", "GREEN",
     "", ""),
    ("M66", "台账历史被砍 (只留最新 20 条: 头部最新、底下删空, 读起来和真的没区别)", "LEDGER", "A18",
     "", ""),
    # A22 两半各一条: 语法用 ast 抓, API 用文本扫抓。少任何一半, 那一半就是"没人钉住"。
    # 注入串必须拼开写: 直写会在本文件里留下点号紧跟方法名的字面量, A22 扫自己时立刻红
    # —— 上一轮"grep XboxApp 零命中"的自指瑕疵换了个马甲又出现一次, 这次由断言自己抓出来。
    ("M67", "py3.8 API 漂移 (往 pool_refresh.py 用 removeprefix 方法: 语法合法、3.8 运行时 AttributeError)", "pool_refresh.py", "A22",
     'if __name__ == "__main__":\n    main()',
     'if __name__ == "__main__":\n    main()\n    _probe = "x".' 'removeprefix("x")'),
    ("M68", "py3.8 语法漂移 (往 pool_refresh.py 写 match 语句: 3.10+ 才有, ast feature_version 必须拦下)", "pool_refresh.py", "A22",
     'if __name__ == "__main__":\n    main()',
     'if __name__ == "__main__":\n    match 0:\n        case 0:\n            main()'),
    # N1/N2 那一族(2026-11-07 外部审计第三轮): A18 前后补过两次, 每次都是"补的洞自己带出洞"。
    # M69 打的是"条数下限被灌水"(砍顶层 + 补子项, 日期行总数守恒), M70 打的是"病名指错方向"
    # (子项被当成兄弟条目比, 报"逆序"而台账没乱)。两条都只动台账, 交付物零字节改动。
    ("M69", "条数灌水 (砍 10 条顶层 + 底部补 10 条更旧的嵌套日期子项: 日期行总数不变)", "LEDGER", "A18",
     "", ""),
    ("M70", "日期子项假红 (最新条目下插一条带日期的嵌套子项: 病名必须是'日期子项'而不是'逆序')", "LEDGER", "A18",
     "", ""),
    # PEP 585 与 removeprefix 同性质(语法合法、3.8 求值即炸), 但 ast feature_version 看不出
    # —— 它就是普通 Subscript。所以 M71 必须存在: 否则那 6 行注解扫描没有任何反证。
    ("M71", "注解泛型下标 (往 pool_refresh.py 写 def f(x: list[int]): 3.8 语法合法、定义时求值即 TypeError)", "pool_refresh.py", "A22",
     'if __name__ == "__main__":\n    main()',
     'if __name__ == "__main__":\n    main()\ndef _probe(x: list[int]):\n    return x'),
    # v2.3.25 是这十几轮里第一次动**运行行为**。改动必须有反证: 否则 C66/C67 只是"跟着代码
    # 一起写的断言", 把校验删掉它们照样绿 —— 那等于没断言。三条各打一个分支。
    ("M72", "池子写入不校验 (把 ip_reject_reason 的结果直接丢掉: 0.0.0.0/环回/私网/CGNAT 全写进池)", "pool_refresh.py", "C66",
     "                why = ip_reject_reason(ip)",
     "                why = None"),
    # 预算判定恒不成立 == 退回 ex.map 全量跑完。这是 F3 的原状, 也是"池子越长越慢"的那条路。
    ("M73", "粗筛无总预算 (判定恒不成立 = 退回 ex.map 全量跑完, 30 个节点最坏 296s)", "xbox-speedtest.py", "C67",
     "            if done >= SCREEN_MIN_DONE and time.perf_counter() - t0 >= budget:",
     "            if False:"),
    # 只留预算、去掉保底: 劣化网络下预算秒空 -> 候选表空 -> 整组被判"无冠军"(活组写成死组)。
    ("M74", "粗筛预算没有保底 (去掉 SCREEN_MIN_DONE: 劣化网络下预算秒空 => 候选表空 => 整组判无冠军)", "xbox-speedtest.py", "C67b",
     "            if done >= SCREEN_MIN_DONE and time.perf_counter() - t0 >= budget:",
     "            if time.perf_counter() - t0 >= budget:"),
    # v2.3.26 是第二次动运行行为(候选来源)。三条各打一个让 C68/C69 变绿的分支:
    # min 退化 / 预算缺失 / 失败当 0 —— 每一条都单独能把"证据链看起来完整"的实现变成谎话。
    ("M75", "RTT 取最后一次 (min-of-N 退化成留最后一次: 一次 1020ms 抖动就把稳态 89MiB/s 的节点判成远)", "xbox-speedtest.py", "C68",
     "            if best is None or d < best:\n                best = d",
     "            best = d"),
    ("M76", "L0 无总预算 (预算判定恒不成立: 261 个节点 × 3 次握手, 池子越长越无边)", "xbox-speedtest.py", "C69",
     "            if done >= RTT_MIN_DONE and time.perf_counter() - t0 >= budget:",
     "            if False:"),
    # 失败节点以 0ms 身份混进候选表: `ms is not None` 那道门是"可达性"的唯一表达,
    # 把它压成 0.0 之后死节点升序排第一, 把真节点挤出前 RTT_TOP —— 而日志里一个 reject 都没有。
    # 注意不能改 `_rtt_min` 的兜底 return: 那会顺带改变失败出口计数, B1 一起红, 分不清病灶。
    ("M77", "握手失败当 0ms (ms 非 None 那道门压成 0.0: 死节点升序排第一, 挤掉真节点)", "xbox-speedtest.py", "C68",
     "            if ms is not None:\n                best[ip] = ms\n",
     "            best[ip] = ms if ms is not None else 0.0\n"),
]


def run_audit(d, extra_env=None):
    env = dict(os.environ)
    env["XBOX_LEDGER"] = LEDGER      # 台账不在 FILES 里, 必须指回真身(否则 A18 只会报"台账不存在")
    if extra_env:
        env.update(extra_env)
    p = subprocess.run([sys.executable, AUDIT, "--dir", d], env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def main():
    tmp = tempfile.mkdtemp(prefix="mutant_")
    fails = []
    skipped = []
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
            if mid in ("M68", "M71") and sys.version_info < (3, 9):
                # 本机就是 py3.8 时, 这两条变异的"漂移"由解释器自己保证被发现:
                # M68 是**硬语法错误**(match 语句), A 段 load(pool_refresh) 先崩(A0/B0/C0 红);
                # M71 是**注解求值即炸**(3.8 里 list 不可下标), 同样崩在 import 那一行。
                # 两者都走不到 A22 —— 也就是说 A22 在 3.8 上没有额外语义, 这条变异在此无意义。
                # 它们只在 3.9+ 的解释器上才需要 A22 提前拦。NAS 实测暴露过 M68 这一形态:
                # 本地 68/68, NAS 报"漏网"(实际红 A0,B0,C0) —— 记账口径按 SKIP 计, 不掺进分母。
                skipped.append(mid)
                print("SKIP %s %s | 本机 py3.8: 漂移在 import 时就炸(A 段 load 先崩), 该变异在本机无意义"
                      % (mid, desc))
                continue
            if mid == "M36":
                # 版本漂移这条不能写死 old: 每次升版本号它就"注入点不存在、自检本身失效"，
                # 于是被算成漏网 —— 这是自检的账，不是交付物的账。改成从 README 现读当前版本。
                _cur = re.search(r"测速主程序（(v[\d.]+)）",
                                 open(os.path.join(SRC, "nas-README.md"), encoding="utf-8").read())
                if _cur:
                    # 新值必须是 **v2.x.y 形式**的旧版本: C46 只收 `v2\.\d+\.\d+`，
                    # 写成 v0.0.1 会被正则过滤掉，清单里剩下的那处仍等于当前版本 => 照样绿（假漏网）
                    old, new = "测速主程序（%s）" % _cur.group(1), "测速主程序（v2.3.8）"
            d = os.path.join(tmp, mid)
            os.makedirs(d)
            for f in FILES:
                shutil.copy(os.path.join(SRC, f), d)
            extra_env = {}
            if fn == "LEDGER" and mid == "M60":
                # 引用假账: 只把台账头部那处检查编号改错, 条目内容与排序**一个字节不动**。
                # 交付物(FILES 那四个)也没动, 所以绿/红只可能来自 A19 —— 这条断言唯一诚实的验法。
                led = os.path.join(tmp, "ledger_%s.md" % mid)
                L = open(LEDGER, encoding="utf-8").read().split("\n")
                _hit = 0
                for i, l in enumerate(L):
                    if old and old in l:
                        L[i] = l.replace(old, new, 1)
                        _hit += 1
                        break
                if not _hit:
                    print("FAIL %s %s | 注入点不存在, 自检本身失效" % (mid, desc))
                    fails.append(mid)
                    continue
                open(led, "w", encoding="utf-8", newline="\n").write("\n".join(L))
                extra_env["XBOX_LEDGER"] = led
            elif fn == "LEDGER":
                # 这一族打的都是台账自己: 把最新一条挪到文件底部 / 换一种写法 / 砍掉历史 / 头部写句真话。
                # 交付物(FILES 那四个)一个字节不动, 所以红只可能来自 A18/A19 —— 这也是这些断言
                # 唯一诚实的验法。M59/M64/M66 是"必须红", M65 是"必须仍绿"(真话不该被判成假账)。
                led = os.path.join(tmp, "ledger_%s.md" % mid)
                L = open(LEDGER, encoding="utf-8").read().split("\n")
                _items = [i for i, l in enumerate(L) if l.startswith("- ")]
                if not _items:
                    print("FAIL %s %s | 台账里没有条目, 自检本身失效" % (mid, desc))
                    fails.append(mid)
                    continue
                if mid == "M59":
                    # 只换相邻两条不够狠: 相邻两条常同一天(甚至同为无时刻条目), 键相等 -> 仍算
                    # 单调不增。真正的病灶是"新条目落到底部", 所以把最新一条挪到最后再验。
                    _l0 = L[_items[0]]
                    del L[_items[0]]
                    _last = max(i for i, l in enumerate(L) if l.startswith("- "))
                    L.insert(_last + 1, _l0)
                elif mid == "M64":
                    # 同一条改成"缩进两格"再挪到底部: 单独测 `startswith("- ")` 那条缝。
                    # 六种写法里缩进两格 / 制表符 / 星号 / 缩进星号都能瞒过它, 真台账 56 条全顶格,
                    # 所以这个缝平时不显形 —— 只有有人用编辑器自动缩进或粘贴时就地生效。
                    _l0 = "  " + L[_items[0]]
                    del L[_items[0]]
                    _last = max(i for i, l in enumerate(L) if l.startswith("- "))
                    L.insert(_last + 1, _l0)
                elif mid == "M65":
                    # 头部声明区(第一个条目之前)写一句**真话**: C0 是段兜底编号, 源码里真实存在。
                    L.insert(_items[0], "> 任何一段崩了由 C0 兜住; B1 数出口。")
                elif mid == "M66":
                    # 只留最新 20 条: 头部最新、底下被删空, 读起来和真的没区别。
                    for _i in sorted(_items[20:], reverse=True):
                        del L[_i]
                elif mid == "M69":
                    # 条数灌水: 砍掉最旧的 10 条顶层, 底部补 10 条更旧的**嵌套**日期子项。
                    # 日期行总数守恒, 灌的正是 `len(_rows)` 那个口子 —— 只有"条数只数最浅那层"
                    # 才能抓; 顺带 `_deep` 非空也是红的另一半。
                    _top = [i for i, l in enumerate(L) if l.startswith("- ")]
                    for _i in sorted(_top[-10:], reverse=True):
                        del L[_i]
                    L.extend(["  - 2025-01-%02d 历史子项 %d" % (10 - _k, _k) for _k in range(10)])
                elif mid == "M70":
                    # 病名: 最新条目下面挂一条带日期的子项(记账里完全正常的写法, "复现于某日")。
                    # 它和兄弟条目没有排序关系; 混进倒序比较就会报"逆序", 把 why 指向错误方向。
                    L.insert(_items[0] + 1, "  - 2025-01-01 复现于上一版")
                open(led, "w", encoding="utf-8", newline="\n").write("\n".join(L))
                extra_env["XBOX_LEDGER"] = led
            elif fn == "SELFTEST":
                # M62 打的是自检脚本自己的头部清单(audit.py 的 A21 读它)。交付物一个字节不动,
                # 所以红只可能来自 A21。被改过的副本放在 tmp 里, 用 XBOX_SELFTEST 指路。
                _sp = os.path.join(tmp, "selftest_%s.py" % mid)
                src = open(os.path.join(HERE, "selftest_audit.py"), encoding="utf-8").read()
                if old not in src:
                    print("FAIL %s %s | 注入点不存在, 自检本身失效" % (mid, desc))
                    fails.append(mid)
                    continue
                open(_sp, "w", encoding="utf-8", newline="\n").write(src.replace(old, new, 1))
                extra_env["XBOX_SELFTEST"] = _sp
            else:
                p = os.path.join(d, fn)
                src = open(p, encoding="utf-8").read()
                if old not in src:
                    print("FAIL %s %s | 注入点不存在, 自检本身失效" % (mid, desc))
                    fails.append(mid)
                    continue
                open(p, "w", encoding="utf-8", newline="\n").write(src.replace(old, new, 1))
            rc, out = run_audit(d, extra_env)
            red = re.findall(r"^FAIL (\S+)", out, re.M)
            if expect_cid == "GREEN":
                # 这类变异不是缺陷, 是**边界不该被跨过**的反证: 改动后审计必须仍然全过。
                # 一旦有人把历史区重新纳进扫描面, 这里就会红 -> 抓到。
                caught = rc == 0
                print(("PASS" if caught else "FAIL") + " %s %s | 审计仍全过 (实际红: %s)"
                      % (mid, desc, ",".join(red) or "无"))
            else:
                caught = rc == 1 and expect_cid in red
                if caught and mid == "M70":
                    # 光红不够, 病名也要对: 子项不是条目, 报"逆序"等于把 why 指向错误方向
                    # (外部审计实测过这条假红: 台账一点没乱, 报出来的却是"逆序")。
                    if "日期子项" not in out:
                        caught = False
                        print("        why 里没有'日期子项' —— 病名指错方向")
                print(("PASS" if caught else "FAIL") + " %s %s | 审计红在 %s (实际红: %s)"
                      % (mid, desc, expect_cid, ",".join(red) or "无"))
            if not caught:
                fails.append(mid)
                print(out[-800:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    _ran = len(MUTANTS) - len(skipped)
    print("\nSELFTEST RESULT:",
          ("审计器可信(%d/%d 缺陷都被抓到%s)" % (len(MUTANTS) - len(skipped) - len(fails), _ran,
                                          ("，本机跳过 %s" % skipped) if skipped else ""))
          if not fails else "有漏网: %s" % fails)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
