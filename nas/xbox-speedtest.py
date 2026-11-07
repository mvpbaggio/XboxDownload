#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Xbox 加速 - NAS 定时自动测速找最快节点并更新路由器 (ZOS/Python3.8 兼容)
v2.3.26 (2026-11-07 第十九轮 —— 候选来源从"省份标签"换成"实测 RTT 排序"; 有运行行为改动):
  池里的省份标签是**人写的**, 会漏; RTT 是本机实测的, 不会漏。
  - 真机打靶(261 / 254 个节点全测): 标签本地 31 个中位 11.3ms, 标签非本地 226 个中位 35.5ms ——
    标签**准**; 但 Cn1 有 15 个、Cn2 有 6 个"标签外省、实测 ≤20ms"的近节点
    (湖南长沙 15.7/19.0ms、福建福州 18.9ms、江西南昌 18.3/18.7ms)。广东到这些地方物理上就 15~19ms,
    它们因为标签写的是外省,**从来没进过粗筛**, 只在"回退全池"或"正好当冠军"时被碰到。
  - 反过来"标签本地但慢"只有 1 例(124.226.72.41), 且是**测量假象**: 5 次握手 [25.7, 25.0, 18.6, 21.4, 23.1],
    median 被一次 1020ms 抖动拉到 1020.8 —— 所以 RTT 必须取 **min-of-3**, 不能取中位。
  - 现在 `rtt_rank` 做 L0 预筛: 全池 TCP443 握手 min-of-RTT_TRIES, 按 ms 升序取前 RTT_TOP=48 个交给粗筛。
    **不设毫秒阈值**: 阈值在链路劣化时会把所有节点一起砍掉, 排序永远给得出前 48 个。
    历史决策过的 21 个节点落在 6.1~36.7ms, 20ms 门会砍掉其中 4 个真实参与者, 25ms 门卡边。
  - 顺带解决可达性: 握手失败的不进候选 —— 真机 Cn1 4 个 / Cn2 14 个死节点此前照样占粗筛名额,
    每个吃满 12s+25s 超时(这就是 Cn2 粗筛 29s 的来源)。
  - L0 与 `_screen` 同规矩: 总墙钟预算 RTT_BUDGET_SEC=30s, 且已完成 >= RTT_MIN_DONE=32 才允许收手。
  - 断言 134 -> 137(C68 min-of-N+可达性剔除+截断 / C69 L0 预算与保底 / C70 标签漏标的近节点也能进候选),
    变异 74 -> 77(M75 取最后一次 / M76 去预算 / M77 失败当 0ms)。
v2.3.25 (2026-11-07 第十八轮 —— 两个"别人写的东西"直接进决策链; 这轮**有运行行为改动**):
  前十七轮改的都是检查器。这轮改的是被检查的东西, 两条都是同一类: 外部输入没有校验/没有上限。
  - F3: `_screen` 用 `ex.map` 提交**全部**节点并等全部跑完。单节点上限不是常数 ——
    连接 DL_TIMEOUT 12s + 读数 QUICK_MAX_SEC 25s = 最坏 37s, 4 并发下 30 个节点最坏 296s,
    而粗筛在决策链最前面、一条决策都不产出。池子还会随 pool_refresh 长(Cn1 已 27 个)。
    现在 submit + as_completed 逐条收, 到 SCREEN_BUDGET_SEC(90s) 且已完成 >= SCREEN_MIN_DONE(6)
    就收手, 未开始的 cancel(py3.8 没有 cancel_futures), 已测的照常排序, 并打一行明说被截断。
    **下限保护不能省**: 劣化网络下每节点都吃满超时, 预算会秒空, 收太早候选表空 => 整组判"无冠军",
    那比"慢"严重(把一个活着的组写成死的)。
  - F2 在 pool_refresh.py: DoH 的 Answer[].data 是上游给的字符串, 此前只有 existing 去重、
    等于"不重复就写"。0.0.0.0 / 127.0.0.1 / 192.168.x.x / 100.64.x.x 都会原样进池, 下游三处
    全当节点用, 最坏被 router_apply 钉进 dnsmasq = 整组断网。现在只放行公网单播 IPv4,
    逐条打 reject + 理由, 与 resolved/appended 同口径。
  - 断言 131 -> 134(C66 写入前校验 / C67 粗筛预算收手 / C67b 保底不吃), 变异 71 -> 74。
v2.3.24 (2026-11-07 第十七轮 —— 台账条目的"层级"与"单位数日期", 加 A22 的 PEP 585 那一半):
  外部审计复核 v2.3.23, 3 条里有 2 条是"上轮补法自己带出来的洞"。仍然**零运行行为改动**。
  - N1: A18 只数日期条目, 没有"层级"概念。带日期的**嵌套子项**(正常记账:"复现于 2025-01-01")
    被当成下一个兄弟条目去比 => 假红, 而且病名报的是"逆序"(why 指错方向, 比不报更糟);
    反过来砍 10 条顶层 + 底部补 10 条更旧的嵌套日期子项, 日期行总数守恒 =>
    条数下限被灌水绕开, 仍然全绿。现在 _rows 带第三元"缩进深度": 最浅那层才是条目
    (排序与条数都只认它), 更深的单独判红并给专门的 why("日期子项") —— 既不冤枉人,
    也不给灌水的口子。
  - N2: 两条日期正则写死 \d{4}-\d\d-\d\d, 于是 `2026-11-1` 这种人手写完全正常的条目**整行隐形**:
    不参与排序、不计条数、连 _nodate 都不进。解析面放宽到 \d{1,2}, 比较面统一补零成 %02d
    (2026-1-7 与 2026-01-07 同秩)。
  - N3: A22 的 ast 那一半看不出 PEP 585 内置泛型下标 —— `def f(x: list[int])` 在 3.8 上
    **语法合法、定义时求值即 TypeError**, 与 removeprefix 同性质。补一遍 Subscript 扫描
    (list/dict/tuple/set/frozenset/type), 由 M71 反证。
  - 变异 M69(条数灌水) / M70(日期子项假红) / M71(注解泛型下标); M70 额外断言 why 里出现
    "日期子项" —— 只红不算过, 病名指错方向等于没修。
  断言 131 -> 131(改的是 A18/A22 的判定体, 没加编号), 变异 68 -> 71;
  本文件与 watchdog.py **零逻辑改动**, 只随交付升版本号。
v2.3.23 (2026-11-07 第十六轮 —— 三个断言自己的解析器不够硬; 外带把 py3.8 兼容钉住):
  外部审计复核 v2.3.22, 提 2 条新 + 1 条"第三轮仍开着"的旧账。三条都是**检查器自己的缝**,
  不是被测代码的 bug —— 所以这一轮仍然**零运行行为改动**。
  - F1 (第三轮提): `audit.py` 判台账条目只看 `startswith("- ")`。缩进两格 / 制表符 / 星号 /
    缩进星号写出来的日期条目会被**整条跳过**, 于是把最新一条改个前缀再挪到文件底部, A18 照样绿。
    六种写法里只有顶格那一种会红。前两轮我只加了注释没动那行(以为真台账全顶格就没事) ——
    这就是它自己注释里警告过的"软跳过等于给了'以后不再检查'的后门"。现在改成:
    **能解析出日期的不管前缀长什么样都纳入倒序比较**(排序才是这条断言的本体),
    形似日期但解析不动的记进 _nodate 判红; 嵌套的无日期子项照旧放过。
  - F5 (新): A19 的 `_real_ids` 只从字面量 `chk("X"` 里取, 而段兜底是 `chk(cid, ...)` 动态传的。
    于是文档里如实写"任何一段崩了由 C0 兜住"会被判成假账 —— A0/B0/C0/D0 在源码里**真实存在**。
    真话被判红和假账被放过是同一个病的两面。补一条 `"X9")` 形态的正则, 只收元组末尾那种,
    不会误收逗号结尾的 `chk("A19", ...)`。
  - F7 (新): A18 的条数下限是 `>=20`, 实测把 56 条砍到最新 20 条**仍然全绿**。
    而"头部最新、底下被删空"读起来和真的没区别 —— 正是 A18 立项要防的那件事的另一半。
    下限提到 50(当前 56, 留一点余量给正当合并)。
  - 顺手加 A22(审计给的选项, 我选了加): NAS 是 3.8.10、本地是 3.13, "本地全绿、NAS 上炸"
    这条路此前只是**碰巧**没走通。现在 ast.parse(feature_version=(3,8)) 扫五个文件的语法,
    再文本扫 3.9+ 的 API(removeprefix / removesuffix / functools 的 cache / zoneinfo / 注解里的竖线 None)。
    把"必须上机才知道"降级成"本地跑一遍就知道"。
  - 变异 M64~M68 各钉一处; 并且**回退修复反证过**: 把三处改回旧写法, M64/M65/M66 立刻集体漏网
    (实际红: 无 / A19 / 无) —— 不这么验一遍, 变异通过只是自证。
  断言 130 -> 131, 变异 66 -> 68; 本文件与 watchdog.py **零逻辑改动**, 只随交付升版本号。
v2.3.22 (2026-11-07 第十五轮 —— 把"引用假账"的扫描面收窄到"当前状态", 边界也要有断言):
  外部审计交来 2 条, 全是低优先级, 但其中一条的结论要改。
  - 意见①" A19 把 README 版本历史变成永久 API 表面" —— **现象成立, 判断不成立**。
    它把这当成"防引用假账的固有代价"; 不必。台账那边早就用"只扫头部声明区"解决过同一个问题,
    README 却扫了全文 —— 两边口径不一致才是病灶。真实后果: 将来重构删掉一个被历史引用过的旧检查,
    A19 会红, 届时要么留一个死检查、要么改历史(违背"只增不改")。现在 README 也只扫到
    "## 版本历史"之前; 边界靠标题定位, 于是新增 **A20**(标题必须有且只有一个 —— 改名不会报错,
    只会让 A19 静默退回扫全文, 那正是它要拦的), 反证是**M63**(往历史里塞两个不存在的编号,
    审计必须**仍然全绿**: 本轮唯一一条 expect GREEN 的变异, 它不是缺陷, 是"边界不该被跨过"的证明)。
  - 意见②" EXT_IDS 是会生长的白名单" —— 成立, 但收窄之后它现在是**空的**: 外部编号只出现在历史区
    (实测 B4 只在版本历史与台账条目里), 而历史区不扫了。机制保留当逃生口。
    同时改掉台账头部那句"B4、S11、S12 这类不算" —— 那行本身就在**被扫的头部**里,
    点名一个不存在的编号等于自己踩自己(A19 当场红在它上面, 这是本轮第二个现场证据)。
  - 自曝③(外部没提): tests/selftest_audit.py 头部那份变异清单停在第 M53 条, 而 MUTANTS 里已有
    60 条 —— 少了 M54~M60 七条, 自检照样 60/60 全过。和 A19 同一类账: 文档声称的清单 ≠ 实际清单。
    新增 **A21**(头部清单必须与 MUTANTS 一一对应, 路径可用 XBOX_SELFTEST 覆盖), 变异 **M62**。
    它上线第一次跑就红在那七条上。
  断言 128 -> 130, 变异 60 -> 63; 本文件与 watchdog.py **零逻辑改动**, 只随交付升版本号。
v2.3.21 (2026-11-07 第十四轮 —— 修"引用假账", 并把它变成可判红的东西):
  另一个 AI 交来 4 条, 取证后 3 条属实 + 1 条方向对而结论错。
  - 属实①: 台账头部写着"排序由 audit.py 的某个编号钉住", 而那个编号**当时并不存在** ——
    那条检查从落进 A 层起就叫 A18
    (起草时另一个名字, 改名后头部没跟)。这类错**没有任何断言能拦**, 因为它不影响任何行为 ——
    断言照常全绿, 只让下一个读的人去找一条不存在的检查, 或以为自己漏跑了什么。所以不是改一个词了事:
    新增 **A19**(台账头部声明区 + README + 两脚本里点名的检查编号必须真实存在于 audit.py,
    外部审计报告自己的编号走 EXT_IDS 豁免), 变异 **M60**(把头部改回那个不存在的编号 -> 只有 A19 红)。
    它是先写断言再改文档的, 上线瞬间就在真文档上红了一次, 这是它有效的现场证据。
  - 属实②: A18 的时间正则边界 —— 小时必须两位纯数字, 分钟允许写 x。已写进台账头部一行。
  - 属实③: 上轮"全仓零引用, grep rc=1"是自指瑕疵 —— 记录这次删除的文字本身现在能被 grep 到。
    措辞改成"删前零命中", 不再声称此刻的全仓状态。
  - **方向对、结论错④**: 意见说"台账有 NAS 镜像, A18 对两侧不一致判红(设计如此)"。前半句该立规矩
    (已写进台账头部与 AI共管协议.md 两处), 后半句不成立: A18 只 open 它读到的那一份
    (XBOX_LEDGER 或默认的项目上一级), 两份都严格倒序时两边都绿。**这个纠错比原意见更值钱** ——
    以为"会红"就不会手动 scp, 镜像会静默落后, 而 04:00 流水线在 NAS 上读的正是镜像那份。
v2.3.20 (2026-11-07 第十三轮 —— 这一轮**代码零改动**, 修的是"状态载体"本身):
  报告 4 条, 逐条取证后: 2 条属实且落在文档/台账(已修), 1 条在 v2.3.19 已经修完(报的是旧快照),
  1 条说法不准确(现象成立、归因错了)。这轮没有一行测速/止血逻辑被碰过, 阈值全原样。
  - 主账: **共管台账长歪了**。文件头写着"新条目加在最上面", 而 2026-10-28 00:3x 之后的 25 条
    (含全部 11-06/11-07 记录)**实际追加在文件底部** —— 只读头部的人会以为最新停在 11-05,
    于是下一个接手的人(人或 AI)按一个过期 48 小时的状态继续改。这比任何一条逻辑 bug 都贵:
    它骗的不是某个判断, 而是"当前是什么状态"这个前提。已把全部 53 条按时间严格重排(最新在最上面),
    并加 **A18** 把排序钉住(条目日期必须单调不增, 跨目录用 XBOX_LEDGER 指路), 变异 M59 反证:
    把最新一条挪到底部, A18 立刻红。
  - 死文件 IP.XboxApp.txt 已删(App 组 2026-10-28 起已移出 POOLS; 删前 grep 零命中,
    删后能命中的只有记录这次删除的文字本身)。
  - "重测超时会不会漏一次体检": 现象成立,**归因不成立** —— 实测(持锁父进程 + 1 秒超时杀子进程):
    子进程被 kill 后 .run.lock 立刻可再取, 父进程退出后 .watchdog.lock 也可再取,**没有锁泄漏**;
    真正会丢体检的是"父进程被 5400s 上限挡住、下一小时 :30 撞上**合法持锁**"那一次, 最多一次。
v2.3.19 (2026-11-07 第十二次真机审计 —— 另一个 AI 复核本仓, 8 条逐条取证):
  这轮修的 4 条里有 2 条**是上一轮自己留下的**: v2.3.18 学到"空 uci 配置 ≠ ssh 不可达"这个教训,
  写进了 README 和本文件, 却只改了本文件那**一处**, 看门狗侧三处命令串原封不动。教训写进文档
  不等于落到代码, 这是第二次犯(第一次是 record_pin 的"基准要改两处")。
  - 本文件只改 1 处(其余在 watchdog.py / tests/audit.py):
    BindIPConn.connect 的 `socket.create_connection((ip, 80))` 改成 `(ip, self.port or 80)`。
    现在 HTTPConnection 默认端口也是 80, 所以今天无害; 但哪天改 HTTPS/8443, 这条会静默连错端口:
    Host 头仍是域名、CDN 照回 200, 测出来的是别的端口的流量, 而且不发任何错。
  - 未动(挂账): 三个测速函数的 conn 在异常路径不 close(CPython 引用计数会收, 真机长跑实测无
    fd 增长); median_of 丢零值口径(改口径已两次引发生产事故)。
v2.3.18 (2026-11-07 第十一次真机审计 —— 复核另一 AI 对**旧副本**做的 13 条审计)
  先说清楚审计对象错了位: 那份报告盯的是 D:/工具安装包/Xbox下载助手/ 下 10 月 13~28 号的五个旧文件
  (watchdog.py 123 行 / xbox-speedtest-nas.py 281 行 / xbox-confirm.py 92 行; 这五个已于同日删除,
  指纹留在 README 已知局限末段), 与部署中的这三个文件
  (842/560 行) 不是同一份代码。13 条里 6 条已在 v2.3.8~v2.3.17 修掉(逐条对账见 README 版本历史
  v2.3.18 条), 3 条属上游 C# 客户端与路由器配置(不在本仓), 4 条是设计取舍(已挂账), **2 条属实且落在本文件/
  看门狗上**, 本轮修的正是这 2 条:
  - 修 bug(空配置被当成"路由器不可达"): `uci show dhcp | grep address` 在没有任何 address 项的路由器上
    退 1(已复现: printf "" | grep address -> rc=1), router_cmd 把非 0 折成 None -> 整轮 return,
    日志却写"ssh 不可达"。换新路由器/配置被清空后这套脚本会永久空转, 且排查方向被带偏(该查配置,
    日志让你查网络)。现改为 `|| true` 并单独打一条"可达但无 address 记录(空配置/首次部署)"。
  - 同一条的看门狗侧(触发子进程无 timeout / 不看退出码)见 watchdog.py v2.3.18: 只打 [trigger]
    不打结果, 子进程被 OOM/异常杀掉和一个成功的重测在日志里长得一模一样。
v2.3.17 (2026-11-07 第十次真机审计, 修的是"标记打的时机"与文档里一句过期五版的假话见 watchdog 侧):
  - 修 bug(标记早于路由器可达性检查): mark_retest() 原来紧跟拿锁之后, 而路由器 ssh 检查在十几行后。
    于是"路由器不通, 本轮整体跳过"这种什么都没测的早退轮也占满 3 小时冷却 —— 路由器瞬断一次,
    看门狗接下来 3 小时都不会重测(而它恰恰是最该立刻重试的时刻)。现在标记挪到 current is not None
    之后: 真的开始测了才算一次重测。冷却的其余语义(收尾再打一次、单写者)不变。
  - 不改阈值、不改测量口径: WIN_RATIO / REPLACE_MIN / RANGE_SPAN / median_of / STEADY_MAX_BYTES 一个未动。
v2.3.15 (2026-11-07 第八次真机审计, 盯的是"上一轮那个补丁到底生效没有"):
  - 修 bug(冷却永远不生效): v2.3.14 的 RETEST_COOLDOWN=45 分钟, 而看门狗的 cron 是**每小时**
    :30 一次 -> 两次触发相隔 3600s > 2700s, 冷却**一次都不会命中**。NAS 上按部署代码现算:
    标记 age=3600 -> retest_cooldown_left()=0 -> 照常重测。也就是说 v2.3.14 声称拦住的
    2026-10-27 那串 20:30:26 / 21:30:35 / 22:30:32 / 23:30:16(间隔 3609/3597/3584s)
    在 45 分钟冷却下会**一模一样地重演**。冷却必须跨过多个 cron 槽位才有意义 -> 3 小时。
  - 修 bug(标记打在开头, 长跑自己吃掉冷却): .last_retest 原来只在拿锁后写一次。一次 3405s 的
    定向重测结束时, 那个标记已经"过期"3405s —— 比整个冷却窗口还长, 下一小时必然放行。
    现在正常结束再写一次(同一个 mark_retest, 仍是单写者): 冷却从"测完"算起。
  - 修 bug(同 IP 分支是唯一不落盘的决策): 另外四个分支都会调 router_apply, 只有"冠军 == 当前 IP"
    这条直接 continue。于是"切了一半"的现场(assets1 已切、assets2 还钉着死 IP)只会打一行
    [warn] 组内域名指向不一致, 永远不修 —— 要等到某天冠军真的赢 15% 才顺带修好。
    改为同 IP 也调一次 router_apply(幂等: 全部已一致时它一个 ssh 都不发, 直接返回 "no change")。
  - 不改阈值、不改测量口径: WIN_RATIO / REPLACE_MIN / RANGE_SPAN / median_of / STEADY_MAX_BYTES 一个未动。
v2.3.14 (2026-11-07, 按外部审计复核结果落的两条补丁):
  - 加冷却(重测无退避): 某组整夜死着时, 每小时 :30 的看门狗都会触发一次定向重测。2026-10-27 真机
    已发生过 —— 20/21/22/23 连续四小时各触发一次全量(3405s/2069s/1284s/1045s)。单组实测 3.71GiB
    -> 24 次/天 ≈ 89GiB, 且大概率测完还是老样子。本程序每次运行(全量 cron / 手动 / 看门狗触发)
    开头写 .last_retest(epoch), 看门狗只读该文件并做 45 分钟冷却: 冷却期内**只止血、不重测**。
    单写者设计: 两边各写一份迟早口径不同(这个项目已经吃过一次"三份门槛没人对齐"的账)。
  - 改 pool_refresh 的解析方式(getaddrinfo -> DoH), 见该文件头: 53 端口解析被钉 IP 名单劫持,
    池子自 10-13 冻结 25 天而日志一直说"appended 0 属正常"。
  - 不改阈值、不改测量口径: WIN_RATIO / REPLACE_MIN / RANGE_SPAN / median_of / STEADY_MAX_BYTES 一个未动。
v2.3.13 (2026-11-07 第七次真机审计, 这次盯"交付态"而不是 diff):
  - 修 bug(已测值靠手写字面量, 两个分支各漏一个 key): v2.3.9 修"冠军一轮测两次"时是在每个
    steady_top_diverse 调用点手写 known={...}, 只覆盖了保持分支。本轮用调用计数桩把八个决策分支
    全跑一遍: **切换分支漏了当前 IP、st_cur==0 分支漏了冠军**, 各被重测 3 轮 = 每处白烧 0.75GiB;
    更糟的是进热备名单的是**第二次**那个抖动值(Cn2 单次差 4 倍), 第二次返回 0 时该成员直接从名单
    静默消失 —— 看门狗失去止血备胎, 而日志只说"名单短就是没有可用热备"。
    改为先算一份 known_measured, 七个调用点共用同一个变量: 逐个打补丁已经漏过两次, 结构上不让它漏。
  - 修断言(语法事实 != 行为): C38b 只检查"每个调用点都传了 known=", 漏 key 照样全绿, 所以上一轮
    全断言 + 全变异都过, 这个洞还在。新增 C45/C45b: 用调用计数桩钉"同一 IP 一轮只测一次, 且名单
    里留的是决策用的第一次值"; M34/M35 各注入一个漏 key 验证审计会红。
  - 不改阈值、不改测量口径: WIN_RATIO / REPLACE_MIN / RANGE_SPAN / median_of 零值过滤一个未动。
v2.3.12 (2026-11-07 第六次真机审计, 全量 185s 后按审计结果修的):
  - 修同一个条件的第二种记账(快测值当稳态基准): "当前IP稳态测不出"有两条路, same-ip 那条 v2.3.8
    已改成 speed=0.0 并写明"不拿快测顶账", 而 alive 那条一直写 round(q_cur, 1) = **快测值**。
    pinned IP 不在 hotspares 里, 看门狗的 load_record 只能回退到 speed -> 拿 93.8 的快测当基准,
    下一小时诚实的 21~40 全判 degraded(21<93.8*0.55=51.6) -> 白触发止血+定向重测, 与 v2.3.8 那条
    注释描述的事故一模一样, 只是换了个分支。现在两条路都写 0.0, 快测值仍留在 router 文案里可读。
  - 门窗槛(三份 20MiB/s 拷贝): REPLACE_MIN / steady_top_diverse(min_steady=) / watchdog.HOTSPARE_MIN_MIBS
    语义相同("低于它不值得换过去"), 注释写了"两处必须一致"却从没人检查。加 C44 钉死三者相等。
  - 不改阈值、不改测量口径: 本轮只修记账, WIN_RATIO / REPLACE_MIN / RANGE_SPAN 一个未动。
v2.3.11 (2026-11-07 第五次真机审计, 两轮全量 185s + 167s 后加的):
  - 加判据(抖动 ≠ 结论被噪声支配): 原来 3 轮极差 >25% 只打"本轮判定受抖动影响", 读起来像"结论是噪声
    掷出来的", 但这两件事无关: 关键看三轮区间和阈值的位置关系。真机同一轮 Cn1 冠军 49.4/47.9/102.1、
    阈值 96.5×1.15=110.98 —— 最好那一轮也差 8.9, 其实稳健; Cn2 30.2/39.6/50.2 vs 114.27 同样稳健。
    新增 decision_note(): 最高值仍 < 阈值 / 最低值已 > 阈值 => "稳健"; 区间横跨阈值才叫"受抖动支配"
  - 记语义(median_of 丢零值 = 两轮失败记满分): 0/0/100 的"中位"是 **100.0**, 因为 median_of 先滤掉 0。
    这是 quick 抗单包丢失的口径, 稳态继承了它 —— 三轮里两轮测不出, 记的是最好那一轮。对 Cn2
    (一晚 0 / 14.1 / ... / 95.x) 等于每次只取最优。**不改语义**(改口径已两次引发生产事故),
    改为在 median_steady 打出"几轮为 0"、在判定措辞里标出"仅 1/3 轮出数"
  - 不改阈值(继续不动 WIN_RATIO 与 REPLACE_MIN): 改口径已两次引发生产事故(v2.3.7 把 Cn2 打瞎、
    v2.3.8 误判死节点), 本轮只把判定成色写进日志, 让事后复盘能区分"噪声"与"证据"
v2.3.9 (2026-11-07 第四次真机审计, 全量实跑 250s 抓到):
  - 修浪费+隐患(冠军一轮测两次): 保持分支里冠军先被 median_steady 测 3 轮当 st_cand, 又在
    steady_top_diverse 里从快测序被捞出来重测 3 轮 —— 同一 IP 一轮烧两份 256MiB×3(=0.75GiB),
    而且第二次的值才是进热备名单的那个: 真机抖动 4 倍(见 v2.3.8 ⑩), 第二次返回 0 时刚测到 95.7 的
    冠军会从热备名单里静默消失(低于 min_steady 被过滤), 名单短了还没人报警。
    改为 steady_top_diverse(known={...}) 复用已测值, 一次实测 8.04GiB 应降到约 7.3GiB
  - 修日志假账(流量乘法对不上): 老日志打 "256MiB × 3 轮/节点 × 4 候选"(=3GiB/组), 而真机每组
    实际 4.02GiB —— 差额正是 steady_top_diverse 之外的 median_steady 调用。改为打印实测节点数,
    让"GiB = 节点数 × 3 × 单次上限"能自己乘平
v2.3.8 (2026-11-07 第三次真机审计, 上一版全量实跑 536s 抓到的真事故):
  - 修事故(Range 跨度超 CDN 容忍值): v2.3.7 把 RANGE_SPAN 提到 STEADY_MAX_BYTES=256MiB, 而 dlassets(Cn2)
    对 256MB/512MB 跨度一律 12s 超时(两个备用 IP 都一样), 8/64/80/160MB 才返 206。后果: 稳态对 Cn2 全 0,
    0 又被"当前IP测不出"分支读成"节点已死", 当场把 211.97.83.41 换成 124.226.72.41。改回 160MiB
  - 修设计(测不出 != 死了): 加 cur_alive(快测复核, 8MB 小块不受大跨度影响) + REPLACE_MIN=20MiB 双闸;
    当前IP真死时, 冠军自己稳态也得 >=20 才配接手, 否则宁可等 watchdog 用热备名单
  - 修 bug(迟滞基准排到队尾): cands 按粗筛分降序, 当前IP 常垫底(截断时被塞进 keep[-1]), 快测预算一超
    第一个跳过的就是它; results 又只留 sp>0, 于是"预算跳过"和"测了是0"无法区分, 下游一律判死。
    改为当前IP排快测队首, q_cur 从完整 quick 表取
  - 文档自我推翻: v2.3.7 条目 ⑥ 的"单连接 107.25 vs 两次连接 93.32 = 15%"同样是单次对比(单列抖动 22%),
    且它的修法把 Cn2 打瞎。README 已标"已推翻"
  - 流量口径: 网卡计数器只能当上界(本轮脚本 6.96GiB vs enp3s0 13.2GiB, 差额是 NAS 上别的容器)
v2.3.7 (2026-11-06 第二次真机审计):
  - 修 bug(测量假货): v2.3.5 号称"读到 EOF 续测", 但 Range 起点写死 0 —— 续测其实是把前 160MB 再下一遍。
    NAS 真机: dl_steady 两次请求 Range 完全相同, 268MB 流量只买到 160MiB 的测量覆盖, 后段(160~295MiB)
    从来没测过。改为按已读字节递增 Range(start=got), 且服务器忽略 Range 返 200 时不再累加(那是重复数据)
  - 修 bug(文档假账): 真机 HEAD 实测 Cn1 测速文件只有 309714493B=295.4MiB,"8 秒稳态"对 Cn1 物理上
    达不到(100MiB/s 下最多 3s), 实际窗口 = min(文件大小, 256MiB)/速率 ≈ 2.6s。README 已按实测改写
  - 修 bug(流量无人记账): 热备稳态候选写死 5 个 = 每组 5×3 轮×256MiB, 一轮全量网卡计数器实测 **9.58GiB**。
    改为 STEADY_CAND_MAX=HOTSPARE_N+1(名单只要 2 个成员, 多测 1 个富余), 并把累计传输量打进日志
  - 修 bug(当前 IP 取法): parse_hist 返回 set 丢写入顺序, main 再用 sorted()[0] 取字典序最小的 IP ——
    有多值残留时会拿一个早该废弃的 IP 当迟滞基准。改为保序 list 取末尾(=最近 add_list), 并对
    "单域名多值残留""组内域名指向不一致"两种现场打警告(那正是 v2.3.3 修的"切了一半"的形态)
v2.3.6 (2026-11-06 NAS 实跑复盘):
  - 修日志假账: 候选构成打印混用截断前后计数, NAS 实跑打出 "候选 16 个 (Top8=8 + 本地保送=31 + 当前=1)"
    (8+31+1≠16), 现明确标 "截断前"
  - 修谎报成功: router_apply 的 dnsmasq restart 失败原来仍返回 True —— v2.3.3 只在 watchdog 修过这条,
    主程序漏了; 现返回 False, 交主流程按 "apply FAILED (kept cur)" 记账
  - 加分段计时: 每组的粗筛/快测耗时各占多少直接打出来(那轮 980s 只能看到总量, 事后无法归因)
v2.3.5 (2026-11-06 真机数据审计):
  - 修 bug(测量口径): dl_steady 号称 8 秒稳态, 实际只下一个 160MB Range, 100MiB/s 链路 1.5 秒读完就退,
    所谓稳态只覆盖开头 1.5 秒且含 TCP 加速段 —— 尾段掉速测不到, 正是"测速 108 实下 40"的成因。
    改为读到 EOF 后继续请求续测, 补 STEADY_MAX_BYTES=256MiB 字节上限(防千兆链路把单次稳态放大到 800MB)
  - 修 bug(热备假分散): op_of 对 pool_refresh 生成的 "(auto1106 assets1.xboxlive.cn)" 返回域名当运营商,
    同一家的 4 个 auto 节点看起来像 4 家 -> 未知运营商统一归 "?" 并只取一个, 分散判断不再被标签骗
  - 修 bug(假账): router_apply 写失败时仍记 ip=新IP/打印 switched, results.json 与路由器实况不符,
    看门狗的劣化基准随之错位 -> 写失败按"没切"记账并标 apply FAILED
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
    (v2.3.26 起入口由 rtt_rank 决定, 这条只作为"保送快测"的优先级, 不再决定谁能进粗筛)
    (省份可配; 全池 248+244 个 IP 粗筛是触发轮 3.2h 长跑的主因)
  - 分组迟滞: WIN_RATIO = {Cn1:1.15, Cn2:1.30} (Cn2 晚间高峰波动大, 稳赢 30% 才换)
  - 热备名单: results.json 每组附 hotspares(冠军+实测过的次优), 供看门狗止血
  - --pool 参数: 只测指定组(看门狗触发定向重测)
流程:
  0) L0 RTT 预筛(2026-11-07): 全池 TCP443 握手 min-of-3, 按 ms 升序取前 RTT_TOP=48 个(见 rtt_rank)
  1) 这 48 个做 8MB 粗筛(并发4) -> Top8
  2) 候选 = Top8 + 本地省份保送(只从 L0 入选者里取) + 当前路由器IP(按粗筛分降序, 封顶 MAX_QUICK_CANDS); 80MB 单连接快测(2轮中位数)排序
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
from concurrent.futures import ThreadPoolExecutor, as_completed

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
STEADY_MAX_SEC = 8.0          # 稳态下载最长 8s
STEADY_MAX_BYTES = 256 * 1024 * 1024   # 稳态字节上限: 8s×60MiB/s 量级, 防千兆链路把单次稳态放大到 800MB
                              # 两条上限同时生效 => 高带宽链路上测不满 8s(如实记录, 见 README 已知局限)
SCREEN_WORKERS = 4
SCREEN_BUDGET_SEC = 90.0      # 粗筛阶段总墙钟预算。单节点上限不是一个常数: 连接 DL_TIMEOUT 12s +
                              # 读数 QUICK_MAX_SEC 25s = 最坏 37s, 4 并发下 30 个节点最坏 296s ——
                              # 而这一整段在决策链**最前面**, 一条决策都不产出。池子还会随 pool_refresh 长
                              # (2026-11-07 已 Cn1 27 / Cn2 15)。到点就用手上已有的排序。
SCREEN_MIN_DONE = 6           # 下限保护: 至少测完这么多个才允许按预算收手。劣化网络下每个节点都吃满
                              # 超时, 预算会秒空 —— 收太早最小候选表会空, 整组被判"无冠军",
                              # 那比"慢"严重得多(那是把一个活着的组写成死的)
RTT_TOP = 48                  # L0 RTT 预筛: 只把握手最快的这么多个交给粗筛。48 不是拍的 ——
                              # 真机 261 个节点里 ≤20ms 的有 45 个(31 个标签本地 + 14 个漏标近节点),
                              # 48 保证一个不漏; 池子剪枝后可降到 32
RTT_WORKERS = 32              # 握手是纯等待, 并发开大不吃 CPU: 261 个 × 3 次实测 5.6s
RTT_TRIES = 3                 # **取 min 不取中位**: 124.226.72.41 五次 [25.7 25.0 18.6 21.4 23.1],
                              # 中位被一次 1020ms 抖动拉到 1020.8, 一个稳态 89MiB/s 的节点会被判"远"
RTT_TIMEOUT = 1.5             # 单次握手上限: 同城/邻省都在 40ms 内, 1.5s 只拦真死节点
RTT_BUDGET_SEC = 30.0         # 与 _screen 同规矩: 单点封不住总时长, 这里给总预算
RTT_MIN_DONE = 32             # 下限保护: 已完成这么多个才允许按预算收手(否则排序样本太小)
DL_TIMEOUT = 12.0
QUICK_ROUNDS = 2              # 快测 2 轮中位数(仅用于排序选冠军候选)
MAX_QUICK_CANDS = 16          # 快测候选上限(按粗筛分降序取): 本地保送在省份节点多时会膨胀到 30+,
                              # 每个龟速候选 2 轮×QUICK_MAX_SEC, 30 个候选实测拖到 1496s
QUICK_BUDGET_SEC = 420.0      # 快测阶段总墙钟预算: 超了就用已测结果(单次 cap 封不住总时长)
STEADY_ROUNDS = 3             # 稳态复测 3 轮中位数(决策依据)
WIN_RATIO = {"Cn1": 1.15, "Cn2": 1.30}   # 分组迟滞: Cn2 晚间波动大, 稳赢 30% 才换
SCREEN_REGIONS = tuple(filter(None, os.environ.get(
    "XBOX_SCREEN_REGIONS", "广东,广西").split(",")))  # 本地省份(环境变量 XBOX_SCREEN_REGIONS 逗号分隔)。
                              # v2.3.26 起**不再决定谁能进粗筛**(入口是 rtt_rank 的实测握手排序),
                              # 只用于"本地保送快测"的优先级 —— 粗筛小块+并发会低估本地节点
HOTSPARE_N = 3                     # results.json 热备名单上限(含冠军)
STEADY_CAND_MAX = HOTSPARE_N + 1   # 稳态候选数上限: 名单只要 HOTSPARE_N-1 个成员, 多测 1 个留富余。
                                   # 原写死 5 = 每组 5×3 轮×256MiB = 3.8GiB, 一轮全量实测 9.58GiB(网卡计数器)
REPLACE_MIN = 20.0                 # 替换者下限: 当前IP判死时, 冠军稳态也得 >= 该值才切
                                   # (2026-11-07 事故: 冠军稳态 14.1 也被切了上去, 把 93 的节点换掉;
                                   #  与 watchdog 的 HOTSPARE_MIN_MIBS 同值, 两处门槛必须一致)
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
        self.sock = socket.create_connection((self._bind_ip, self.port or 80), timeout=self.timeout)


RANGE_SPAN = 160 * 1024 * 1024   # 单个 Range 请求的字节跨度。**不能**直接对齐 STEADY_MAX_BYTES:
                                 # 2026-11-07 真机实测 dlassets(Cn2) 对 span=256MB 直接 12s 超时(两个节点都一样),
                                 # 160MB 才正常(96~103MiB/s)。span 大于 CDN 容忍值时 dl_steady 全返回 0,
                                 # 又被"当前IP测不出"分支读成"节点已死" —— 一次误判就能把 93 的节点换成 14 的。


def _req(conn, host, path, start=0, span=RANGE_SPAN):
    """发 GET。start 是这次请求的**起始字节偏移** —— v2.3.5 的续测把 Range 写死成 0-167772159,
    读到 EOF 后第二次请求又从字节 0 开始, 等于把前 160MB 再下一遍: 后段永远测不到, 还白花 160MB 流量。
    2026-11-06 NAS 真机: dl_steady 两次请求的 Range 完全相同, 268MB 流量只买到 160MiB 的测量覆盖。"""
    conn.request("GET", path, headers={
        "Host": host, "Range": "bytes=%d-%d" % (start, start + span - 1),
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


_STEADY_TRAFFIC = [0]   # 本轮稳态累计下载字节(只用于日志: 一轮全量到底吃了多少流量, 此前没人算过)
_STEADY_NODES = [0]     # 本轮稳态实际开测的节点数(不是"候选数"): 日志里的流量乘法要能自己乘平,
                        # 老版打"×4 候选"(=3GiB/组)而真机 4.02GiB —— 差额是 median_steady 的独立调用
_STEADY_ROUNDS_MEM = {}  # {ip: [三轮原始值]}: 只留内存不落盘, 供"判定稳健性"复盘。
                         # 2026-11-07 01:00 全量: Cn1 冠军 49.4/47.9/102.1、阈值 96.5×1.15=110.98,
                         # 日志只写"本轮判定受抖动影响", 读起来像"结论是噪声决定的" —— 但最好那一轮
                         # 也过不了 110.98, 其实稳健。离散度和"抖动能不能翻掉结论"是两件事, 必须分开判

RETEST_MARK_NAME = ".last_retest"   # 与 watchdog.RETEST_MARK_NAME 必须同名(冷却看的就是它)
                                    # 路径在调用时用 BASE 拼: 模块导入时算死的话, 测试改 BASE 就重定向不动


def mark_retest():
    """记下"本轮跑过", 供 watchdog 的重测冷却判断。

    没有这个标记时: 某组整夜死着(整区段故障/热备全灭), 每小时 :30 的看门狗都会触发一次定向重测。
    2026-10-27 真机已发生: 20/21/22/23 连续四小时各触发一次全量(3405s/2069s/1284s/1045s)。
    单组实测 3.71GiB -> 24 次/天 ≈ 89GiB, 且大概率测完还是老样子。
    main 里调用两次(开头 + 正常结束): 只打在开头时, 长跑会把自己的冷却吃掉(见 main 尾部注释)。
    单写者: 只有本程序写, 看门狗只读 —— 两边各写一份迟早口径不同。
    """
    try:
        with open(os.path.join(BASE, RETEST_MARK_NAME), "w", encoding="utf-8") as fh:
            fh.write("%d\n" % int(time.time()))
    except Exception:
        pass      # 标记写不进去(只读盘/权限)绝不能影响测速本身


def dl_steady(ip, host, path, max_sec=STEADY_MAX_SEC, max_bytes=STEADY_MAX_BYTES):
    """稳态: 持续下载, 同时受 max_sec 与 max_bytes 两条上限约束, 摊平 CDN 突发。
    v2.3.5 曾以为"一个 160MB Range 1.5 秒读完就退"是主因, 续测即可; 2026-11-06 NAS 真机HEAD 实测
    才发现两件事: ①续测的 Range 起点写死为 0, 第二次是把前 160MB 再下一遍(268MB 流量只覆盖 160MiB);
    ②Cn1 测速文件 extraextralarge.txt 只有 309714493B=295.4MiB, 100MiB/s 下物理上最多 3s ——
    "8 秒稳态"对 Cn1 是达不到的, 实际窗口 = min(文件大小, STEADY_MAX_BYTES) / 速率。
    现续测按已读字节递增 Range; 服务器若忽略 Range 返 200 则不再累加(那是从头重传的重复数据)。"""
    try:
        t0 = None      # 计时起点 = 首次响应头到达。TCP 三次握手/首字节延迟不是吞吐, 不该算进窗口。
                       # (2026-11-06 单次对比曾看到 dl_steady 92.10 vs watchdog 99.90, 但交替测 4 轮均值为
                       #  97.1 vs 97.9 = 0.8%, 而单列抖动本身就有 22% —— 那 7.8% 是抖动, 不是系统偏差。
                       #  仍保留这个起点, 理由是两边口径必须一致, 便于 hotspares.steady 直接比对)
        got = 0
        while got < max_bytes:
            conn = BindIPConn(host, ip, DL_TIMEOUT)
            _req(conn, host, path, start=got)   # 续测从 got 继续: 否则后段永远测不到, 且重复下同一段
            resp = conn.getresponse()
            if resp.status not in (200, 206):
                conn.close()
                break  # 非 200/206 与异常走同一出口, 不各自 return 0(单一失败出口)
            if resp.status == 200 and got:
                conn.close()
                break  # 服务器忽略 Range: 这一轮读的是从头重传的重复数据, 累加会把流量当成绩
            if t0 is None:
                t0 = time.perf_counter()
            n0 = got
            while time.perf_counter() - t0 < max_sec and got < max_bytes:
                b = resp.read(65536)
                if not b:
                    break
                got += len(b)
            conn.close()
            if got == n0:
                break  # 这一轮一个字节都没拿到, 不要重连自旋
            if time.perf_counter() - t0 >= max_sec:
                break  # 时间用尽, 不要再建新连接
        el = (time.perf_counter() - t0) if t0 is not None else 0.0
        _STEADY_TRAFFIC[0] += got
        return got / (1048576 * max(el, 0.1)) if got else 0.0
    except Exception:
        return 0.0


def median_of(vals):
    """去掉 0 值后取中位数。0 = "这一轮没测出", 不是"0 MiB/s 的吞吐", 混进中位数会把一次
    偶发失败摊成全轮变慢 —— 这是 quick(抗单包丢失)与 steady 共用的口径。
    但代价必须记住(2026-11-07 审计): **0/0/100 的中位数是 100.0**。也就是说三轮里两轮测不出,
    记的是单轮满分; 对 Cn2 这种一晚能测到 0 / 14.1 / ... / 95.x 的节点, 等于每次只取最好的那一轮。
    改口径在本项目已两次引发生产事故, 所以这里**不改语义**, 只让 median_steady 打出"几轮为 0"。"""
    good = [v for v in vals if v > 0]
    return statistics.median(good) if good else 0.0


def median_quick(ip, host, path):
    return median_of([dl_speed(ip, host, path, QUICK_CHUNK) for _ in range(QUICK_ROUNDS)])


def median_steady(ip, host, path):
    """3 轮取中位数, 并把离散度打进日志。
    2026-11-07 实测 Cn2 同一个 IP 在一晚之内测到 0 / 14.1 / 20.6 / 21.3 / 23.9 / 26.8 / 82.6 / 93.8 / 95.x,
    3 轮内部也会出现 30.7 / 50.9 / 91.5 这种跨度 —— 迟滞阈值 1.3 远小于这个抖动幅度,
    也就是说 Cn2 上的"该不该换"很大程度是噪声在决定。日志必须把这个事实留在现场,
    否则事后只看中位数会以为决策依据很硬。"""
    _STEADY_NODES[0] += 1
    rs = [dl_steady(ip, host, path) for _ in range(STEADY_ROUNDS)]
    med = median_of(rs)
    _STEADY_ROUNDS_MEM[ip] = rs
    lo, hi = min(rs), max(rs)
    zeros = sum(1 for v in rs if v <= 0)
    if zeros:
        # median_of 会丢掉 0 值(见函数注释), 所以 0/0/100 的"中位"是 100.0 —— 三轮里两轮测不出,
        # 记的却是单轮满分。这不是笔误, 是 quick 抗单包丢失的同一套逻辑被稳态继承了。
        # Cn2 一晚能测到 0 / 14.1 / ... / 95.x, 这条日志就是为了让"基於几轮"可见, 而不是看起来像 3 轮都成。
        print(f"  [t] 稳态零值 {ip}: {'/'.join('%.1f' % x for x in rs)} -> {zeros}/{len(rs)} 轮为 0,"
              f" 中位只取非零轮 = {med:.1f}", flush=True)
    if med > 0 and (hi - lo) > med * 0.25:
        # 别写 "极差 22.8 > 25%": 22.8 是绝对 MiB, 25% 是比例, 这行读起来像个假不等式
        # (2026-11-07 00:53 真机日志就是这样, 22.8 > 79.9×25%=20.0 才是真正比较的两项)
        print(f"  [t] 稳态离散 {ip}: {'/'.join('%.1f' % x for x in rs)} -> 中位 {med:.1f}"
              f" (极差 {hi - lo:.1f} > 中位×25%={med * 0.25:.1f}, 本轮判定受抖动影响)", flush=True)
    return med


def decision_note(cand, thr):
    """把"抖动能不能翻掉结论"直接算出来。返回 ""(判定稳健, 无需多说) 或一句结论。
    离散度只说明"三轮不一致"; 判定是否可信取决于这个区间和阈值的**位置关系**:
      - 三轮的最高值都够不到 thr => 再抖也换不了, 结论稳健
      - 三轮的最低值都超过 thr => 再抖也换, 结论同样稳健
      - 区间横跨 thr => 换不换取决于抽到哪一轮, 这才是"由噪声决定"
    2026-11-07 01:00 真机: Cn1 冠军 49.4/47.9/102.1 vs 阈值 110.98 -> 最高值仍差 8.9, 稳健;
    Cn2 冠军 30.2/39.6/50.2 vs 阈值 114.27 -> 稳健。两条都只打了"受抖动影响", 说不出这个区别。"""
    rs = _STEADY_ROUNDS_MEM.get(cand) or []
    if len(rs) < 2 or thr <= 0:
        return ""
    good = [v for v in rs if v > 0]
    tag = "" if len(good) == len(rs) else f"仅 {len(good)}/{len(rs)} 轮出数, "
    med = median_of(rs)
    lo, hi = min(rs), max(rs)
    if med <= 0 or (hi - lo) <= med * 0.25:      # 本来就够稳, 不刷屏
        return ""
    if hi < thr:
        return f"稳健({tag}最好一轮 {hi:.1f} 仍 < 阈值 {thr:.1f}, 抖不动)"[:200]
    if lo >= thr:
        return f"稳健({tag}最差一轮 {lo:.1f} 已 > 阈值 {thr:.1f}, 抖不掉)"
    return (f"**受抖动支配**({tag}三轮 {lo:.1f}~{hi:.1f} 横跨阈值 {thr:.1f}, "
            f"换不换取决于抽到哪一轮, 下轮可能翻转)")


def parse_hist(current):
    """OpenWrt uci 把同一 option 的多个 list 合并一行, 仅首条带 address= 前缀,
    其余为裸引号串 -> 用引号包裹的完整 /domain/ip 全量匹配。
    返回 {domain: [ip, ...]}, **保留写入顺序** —— main 取当前 IP 要用最后一个(list 末尾=最近 add_list),
    原来用 set 丢顺序、再 sorted()[0] 取字典序最小的那个, 有多值残留时会取到一个早就该废弃的 IP。"""
    hist = {}
    for m in re.finditer(r"'/([^/]+)/([0-9.]+)'", current):
        lst = hist.setdefault(m.group(1), [])
        if m.group(2) not in lst:
            lst.append(m.group(2))
    return hist


def _rtt_min(ip, port=443, tries=RTT_TRIES, timeout=RTT_TIMEOUT):
    """单节点 TCP 握手耗时, 取 N 次里的最小值; 全失败返回 None(不可达)。

    **min 不是 median**: 单次握手会被瞬时抖动骗 —— 124.226.72.41(广西南宁, 稳态 89MiB/s)
    五次 [25.7 25.0 18.6 21.4 23.1] 中位 23.1, 但如果其中一次撞上 1020ms, median 直接变 1020.8,
    一个真节点会被判成"远"。min 的代价是偶尔高估某个抖动节点(它多进一次粗筛), 可接受。
    """
    best = None
    for _ in range(tries):
        t = time.perf_counter()
        try:
            s = socket.create_connection((ip, port), timeout=timeout)
            d = (time.perf_counter() - t) * 1000.0
            s.close()
            if best is None or d < best:
                best = d
        except Exception:          # 超时/拒绝/不可达一律按不可达, 不区分原因(排序只看数值)
            continue
    return best


def rtt_rank(rows, port=443, top=RTT_TOP, budget=RTT_BUDGET_SEC):
    """L0 预筛: 全池 TCP443 握手 min-of-N, 返回 [(ms, ip, tag)] 升序前 top 个。

    为什么排序而不是阈值: 阈值的语义是"绝对快慢", 链路一劣化所有节点一起超线、一次砍光;
    排序永远给得出前 top 个, 只是绝对值变大。历史决策过的 21 个节点落在 6.1~36.7ms,
    20ms 门会砍掉 4 个真实参与者(20.4 / 25.4 / 25.8 / 31.0 / 36.7 里的后四个), 25ms 门卡边。
    为什么不用省份标签: 标签是人写的, 会漏 —— 真机实测 21 个"标签外省但 ≤20ms"的近节点
    (长沙/南昌/福州)因为标签不是广东广西, 从来没进过粗筛。RTT 不依赖任何人维护。
    顺带解决可达性: 握手失败的不进候选(真机 Cn1 4 个 / Cn2 14 个死节点此前照样占粗筛名额,
    每个吃满 12s+25s 超时 —— 这正是 Cn2 粗筛 29s 的来源)。
    预算与保底同 _screen: 到 RTT_BUDGET_SEC 且已完成 >= RTT_MIN_DONE 才收手, 否则排序样本太小。
    """
    uniq = []
    seen = set()
    for ip, tag in rows:
        if ip in seen:              # 池文件理论上已去重, 但同一 IP 两条记录会白烧 3 次超时
            continue
        seen.add(ip)
        uniq.append((ip, tag))
    if not uniq:
        return []
    tag_of = dict(uniq)
    t0 = time.perf_counter()
    best = {}
    fut_ip = {}
    ex = ThreadPoolExecutor(max_workers=RTT_WORKERS)
    try:
        for ip, _tag in uniq:
            fut_ip[ex.submit(_rtt_min, ip, port)] = ip
        done = 0
        for fut in as_completed(fut_ip):
            ip = fut_ip[fut]
            try:
                ms = fut.result()
            except Exception:
                ms = None                    # 线程里抛的一律按不可达, 与 _rtt_min 自己的兜底同口径
            done += 1
            if ms is not None:
                best[ip] = ms
            if done >= len(uniq):
                break
            if done >= RTT_MIN_DONE and time.perf_counter() - t0 >= budget:
                print(f"  RTT 预筛预算 {budget:g}s 用尽, 余 {len(uniq) - done} 个节点跳过"
                      f"(用已测 {done} 个排序)", flush=True)
                break
    finally:
        for f in fut_ip:
            f.cancel()                        # py3.8 没有 shutdown(cancel_futures=True)
        ex.shutdown(wait=False)               # 正在跑的 RTT_WORKERS 个自然结束(最多 RTT_TRIES×RTT_TIMEOUT)
    out = sorted((ms, ip, tag_of.get(ip, "")) for ip, ms in best.items())
    return out[:top]


def _screen(rows, host, url_path, budget=SCREEN_BUDGET_SEC):
    """8MB 粗筛, 返回 [(speed, ip)] 降序。

    **总墙钟预算**: 原实现 `ex.map(...)` 会把**全部**节点都跑完才返回 —— 单节点最坏 37s
    (连接 12s + 读数 25s), 4 并发 30 个节点就是 296s, 而这段在决策链最前面、一条决策都不产出。
    单节点 cap 封不住总时长, 所以在这里给总预算: 逐条 as_completed 收, 到点且已完成 >= SCREEN_MIN_DONE
    就收手, 未开始的 cancel 掉, 已测的照常排序。
    收手必须**打一行**, 否则日志里只有一个偏小的 "[t] 粗筛 Ns", 读起来像"测完了但都很慢"。
    """
    ips = [ip for ip, _ in rows]
    uniq = list(dict.fromkeys(ips))          # 池文件理论上已去重, 但同一 IP 两条记录会重复烧一次超时
    if not uniq:
        return []
    t0 = time.perf_counter()
    speeds = {}
    fut_ip = {}
    ex = ThreadPoolExecutor(max_workers=SCREEN_WORKERS)
    try:
        for ip in uniq:
            fut_ip[ex.submit(dl_speed, ip, host, url_path, SCREEN_CHUNK)] = ip
        done = 0
        for fut in as_completed(fut_ip):
            ip = fut_ip[fut]
            try:
                sp = fut.result()
            except Exception:
                sp = 0.0                      # 线程里抛的一律按 0, 与 dl_speed 自己的兜底同口径
            speeds[ip] = sp
            done += 1
            if done >= len(uniq):
                break
            if done >= SCREEN_MIN_DONE and time.perf_counter() - t0 >= budget:
                # 用 :g 不用 :.0f —— 真机 py3.8 实测 budget=0.5 时 :.0f 打出"预算 0s 用尽",
                # 读起来像 bug 而不是"预算很小"。默认 90 打出来仍然是 90。
                print(f"  粗筛预算 {budget:g}s 用尽, 余 {len(uniq) - done} 个节点跳过"
                      f"(用已测 {done} 个排序)", flush=True)
                break
    finally:
        for f in fut_ip:
            f.cancel()                        # py3.8 没有 shutdown(cancel_futures=True)
        ex.shutdown(wait=False)               # 正在跑的 4 个自然结束(最多 37s), 不阻塞主流程
    return sorted(((sp, ip) for ip, sp in speeds.items() if sp > 0), reverse=True)


_NOT_OP_RE = re.compile(r"^(?:[0-9.]+|[a-z0-9-]+(?:\.[a-z0-9-]+)+)$")  # 纯 IP/数字 或 域名


def op_of(tag):
    """从 "(中国广东佛山 电信)" 这类标签里取运营商名, 供热备分散判断。
    2026-11-06 审计: pool_refresh 生成的标签形如 "(auto1106 assets1.xboxlive.cn)", 末段是**域名**;
    原实现直接取末段 -> 同一家运营商的 4 个 auto 节点被当成 4 家运营商, "分散"完全是假的。
    取不到运营商就返回 ""(未知), 由 steady_top_diverse 按同一家处理。"""
    t = (tag or "").replace("(", " ").replace(")", " ").strip()
    if not t:
        return ""
    last = t.split()[-1]
    if _NOT_OP_RE.match(last.lower()):
        return ""
    return last


def steady_top_diverse(results, exclude, host, url_path, tags, n=2, avoid_ops=(), min_steady=20.0,
                       known=None):
    """稳态复测热备, 按运营商分散 + 可用性下限过滤:
    - 分散: 同运营商段级故障时整份名单会一起失效。2026-11-06 20:30 实测三个热备全是广东电信,
      同时掉到 1.0/1.4MiB/s, 止血退化成 25 分钟全量重测 -> 至少留一条别的运营商的活路。
    - 下限: 分散不能拿烂节点凑数(首版曾把 4.3MiB/s 的"别家运营商"塞进名单, watchdog 20MiB/s 门槛过不了,
      纯占位)。低于 min_steady 的一律不进名单, 名单短就是真的没有可用热备。
    - known: 本轮**已经测过**的 {ip: steady}, 直接采用, 不再重测。2026-11-07 00:44 全量实跑(250s)发现:
      保持分支里冠军先被 median_steady 测成 st_cand, 又被这里从快测序捞出来重测一遍 —— 同一 IP 一轮
      两份 256MiB×3 = 0.75GiB 白烧; 更糟的是**进热备名单的是第二次那个值**: Cn2 单次抖动能到 4 倍
      (v2.3.8 ⑩), 第二次返回 0 时刚测到 95.7 的冠军直接低于 min_steady 被过滤掉, 名单静默少一个人。
    只对快测序前 STEADY_CAND_MAX 个候选做稳态(每个 3 轮×256MiB), 再多纯烧时间与流量。"""
    known = known or {}
    measured = []
    for _sp, ip in results:
        if len(measured) >= STEADY_CAND_MAX:
            break
        if ip in exclude:
            continue
        if known.get(ip, 0.0) > 0:      # 复用已测值(含 0 的不复用: 0 是"这轮测不出", 留着让下面重测一次)
            measured.append({"ip": ip, "steady": round(float(known[ip]), 1),
                             "_op": op_of(tags.get(ip, ""))})
            continue
        measured.append({"ip": ip,
                         "steady": round(median_steady(ip, host, url_path), 1),
                         "_op": op_of(tags.get(ip, ""))})
    measured.sort(key=lambda x: x["steady"], reverse=True)
    usable = [m for m in measured if m["steady"] >= min_steady]
    if measured and not usable:
        print(f"  [warn] 实测 {len(measured)} 个候选稳态均 < {min_steady:.0f}MiB/s, 无可信热备(名单留空)", flush=True)
    out, seen = [], set(k or "?" for k in avoid_ops)   # avoid_ops: 冠军已占用的运营商, 不与冠军撞车
    for m in usable:                 # 第一轮: 一运营商一个
        key = m["_op"] or "?"        # 运营商未知的(auto 标签)也归同一家, 否则"4 个域名"就是 4 家
        if key in seen:              # 第一轮就让它只占一个位, 别把真正不同的运营商挤出名单
            continue
        out.append(m)
        seen.add(key)
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
    t_start = time.perf_counter()   # 分段计时: 2026-11-06 那轮只知道总量 980s, 说不清粗筛/快测各占多少
    full_rows = load_ips(path)
    rows = full_rows
    tag_of = {ip: tag for ip, tag in full_rows}
    # L0: RTT 预筛。候选来源从"省份标签 + autoMMDD"换成"实测握手最快的前 RTT_TOP 个" ——
    # 标签会漏(真机 21 个标签外省但 ≤20ms 的近节点从来没进过粗筛), RTT 不会漏, 也不需要人维护。
    # 顺序不能反: 先 RTT 再 8MB 粗筛 —— 粗筛每个节点要拉 8MB, RTT 只握手, 快的先过筛省时间。
    prox = rtt_rank(rows)
    if prox:
        msl = [ms for ms, _, _ in prox]
        inl = sum(1 for _, ip, _ in prox if any(r in tag_of.get(ip, "") for r in SCREEN_REGIONS))
        print(f"[{name}] RTT 预筛: 入选 {len(prox)}/{len(rows)} 个 "
              f"(最慢 {msl[-1]:.0f}ms, 中位 {msl[len(msl) // 2]:.0f}ms, "
              f"其中 {','.join(SCREEN_REGIONS)} 标签 {inl} 个)", flush=True)
        screen_rows = [(ip, tag) for _, ip, tag in prox]
    else:
        # L0 全都不通(链路真断/池文件全脏): 退回全池粗筛, 让粗筛自己的超时兜底。
        # 不能在这里返回空 —— 那等于"一轮网络抖动就把整组判成无冠军"。
        print(f"[{name}] RTT 预筛无可达节点, 退回全池粗筛", flush=True)
        screen_rows = rows
    ranked = _screen(screen_rows, host, url_path)
    t_screen = time.perf_counter() - t_start
    top = [ip for _, ip in ranked[:SCREEN_N]]
    # 本地省份节点保送确认: 粗筛小块+并发会低估本地节点, 防止漏选(本地运营商节点延迟优势最大)。
    # 保送名单只从 L0 入选者里取 —— 标签不能再把慢节点塞进快测, 但可以在入选者里优先。
    prov = [(ip, tag) for ip, tag in screen_rows if any(r in tag for r in SCREEN_REGIONS)]
    gd = [ip for ip, _tag in prov]
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
    # 构成必须标"截断前": 截断后 cands 只剩 MAX_QUICK_CANDS 个, 拿原始 len(top)/len(gd) 拼在一起
    # 会打出 "候选 16 个 (Top8=8 + 本地保送=31 + 当前=1)" 这种 8+31+1=40≠16 的假账(NAS 2026-11-06 23:09 实跑踩到)
    print(f"  候选 {len(cands)} 个 (截断前: Top{SCREEN_N}={len(top)} + 本地保送={len(gd)} + 当前={1 if current_ip else 0})", flush=True)
    quick = {}
    t_q = time.perf_counter()
    # 当前 IP 必须第一个测: 它是迟滞基准, 缺了就只能当"死"。
    # 而 cands 是按粗筛分降序排的, 当前 IP 常垫底(截断时甚至被放在 keep[-1]), 预算一超就被 break 跳过;
    # results 又只保留 sp>0 的项 —— "预算跳过" 与 "测了是 0" 在 results 里长得一模一样,
    # main 一律读成 cur_alive=False。2026-11-07 之前这条路径没被踩到只是因为预算够用, 属于运气。
    order = ([current_ip] if current_ip in cands else []) + [i for i in cands if i != current_ip]
    for ip in order:
        left = QUICK_BUDGET_SEC - (time.perf_counter() - t_q)
        if quick and left <= 0:
            print(f"  快测预算 {QUICK_BUDGET_SEC:.0f}s 用尽, 余 {len(order) - len(quick)} 个候选跳过(用已测最优)",
                  flush=True)
            break
        quick[ip] = median_quick(ip, host, url_path)
    t_quick = time.perf_counter() - t_start - t_screen
    print(f"  [t] 粗筛 {t_screen:.0f}s / 快测 {t_quick:.0f}s / 快测出数 {len(quick)} 个", flush=True)
    # 预算提前 break 时 quick 里没有剩余候选的键, 必须按 quick 取而不是按 cands(否则 KeyError)
    results = sorted(((sp_, ip_) for ip_, sp_ in quick.items() if sp_ > 0), reverse=True)
    if not results:
        return None
    sp, ip = results[0]
    print(f"  -> 快测冠军 {ip} {tag_of.get(ip, '').strip()[:28]} median={sp:.1f}MiB/s", flush=True)
    return {"group": name, "ip": ip, "speed": round(sp, 1), "quick": quick,
            "region": tag_of.get(ip, "").strip()[:40], "rows": full_rows,
            "tag_of": tag_of, "ranked": results,
            "_t": {"screen": round(t_screen, 1), "quick": round(t_quick, 1)}}


def router_cmd(cmd):
    """在路由器上执行命令, 返回 stdout 或 None"""
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
                            "-i", SSH_KEY, f"root@{ROUTER}", cmd],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=30)
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
        if set(olds or []) == {ip}:
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
        # uci 已提交但 DNS 没生效 = 这一刀没落地, 不能返回成功: 否则 results.json/speed.log 记成"已切换",
        # 而下一轮从 uci 读到的"当前IP"已是新值, 差异消失, 假账会被永久固化(与 watchdog 同一条规矩)
        return False, "uci committed but dnsmasq restart FAILED"
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
    # `|| true` 不是装饰: `grep address` 在**干净路由器**上没有匹配行就退 1(换新路由器、或上一次
    # del_list 把最后一条清空之后), 而 router_cmd 把"非 0 退出码"一律折成 None —— 于是"空配置"与
    # "ssh 不通"变成同一个 None, 本轮直接 return, 日志却写"ssh 不可达"。这种路由器会永久空转,
    # 排查方向被彻底带偏。2026-11-07 外部审计指出, 本机复现: printf "" | grep address -> rc=1。
    current = router_cmd("uci show dhcp | grep address || true")
    if current is None:
        print("[env] 路由器 ssh 不可达, 本轮整体跳过(测速结果无法应用, 不白烧 10 分钟带宽)", flush=True)
        return
    if not current:
        # 与上面那条分开说: 这里是"ssh 通、但一条 address 都没有", 属于首次部署/配置被清空
        print("[env] 路由器可达但没有任何 address 记录(空配置/首次部署): 按无钉住处理, 本轮照常测速并写入",
              flush=True)
    # 标记打在**路由器可达之后**: 什么都不测的早退轮不该占满 3 小时冷却。v2.3.16 把它放在拿锁之后,
    # 于是路由器瞬断一次, 看门狗接下来 3 小时都不重测 —— 而那恰恰是最该立刻重试的时刻。
    # 收尾还会再打一次(冷却从"测完"算), 单写者不变。
    mark_retest()
    hist = parse_hist(current) if current else {}
    cur_ip = {}
    for name, _p, _h, _u, doms in POOLS:
        seen = {}                 # {domain: [ip...]} 该组每个域名当前钉着谁(list 末尾=最近写入)
        for d in doms:
            if hist.get(d):
                seen[d] = hist[d]
        if not seen:
            cur_ip[name] = None
            continue
        cur_ip[name] = seen[doms[0]][-1] if doms[0] in seen else list(seen.values())[0][-1]
        # 残留检测: uci 里同一域名挂多个 IP(v2.3.3 之前"只切首域名"时代的遗留), dnsmasq 行为不确定;
        # 组内各域名指向不一致则是"切了一半"的现场 —— 这两种都要在日志里说出来, 不能悄悄取一个当真相
        multi = {d: v for d, v in seen.items() if len(v) > 1}
        if multi:
            print(f"[warn] {name}: 这些域名挂着多个 IP(残留), 取最近写入的那个: "
                  + " ".join(f"{d}={'/'.join(v)}" for d, v in multi.items()), flush=True)
        odd = {d: v[-1] for d, v in seen.items() if v[-1] != cur_ip[name]}
        if odd:
            print(f"[warn] {name}: 组内域名指向不一致(切了一半?): "
                  + " ".join(f"{d}={v}" for d, v in odd.items())
                  + f" 基准取 {doms[0]}={cur_ip[name]}", flush=True)
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
        quick = r.pop("quick", {})   # 完整快测表(含 0 分与"预算跳过"的区别): q_cur 只能从这里取
        r.pop("rows", None)        # 全池明细不落盘(看门狗只用 hotspares, 白占体积)
        tags = r.pop("tag_of", {})   # 热备分散要用运营商标签, 但不落盘
        r.pop("_t", None)            # 分段计时只给人看, 不进 results.json
        if cand == cur_ip[name]:
            # 冠军自己的稳态必须测: 只存快测中位数会让看门狗拿它当劣化基准(快测虚高 -> 劣化误判),
            # 且 hotspares[0]=冠军 是 load_record 的取值前提
            st_self = median_steady(cand, host, url_path) if cand else 0.0
            head = [{"ip": cand, "steady": round(st_self, 1)}] if st_self > 0 else []
            if head:
                r["speed"] = head[0]["steady"]
                r["router"] = "no change (same ip)"
            else:
                # 稳态测不出(超时/被节点限速) != 快测那个数就是稳态。
                # 2026-11-07 00:29 实跑踩到: Cn2 快测中位 93.8、稳态 3 轮全 0, 而 r["speed"] 还留着
                # test_pool 写的 93.8 —— 看门狗拿它当基准, 下一小时自己一测 21 就判 degraded(21<93.8*0.55),
                # 白触发一次止血/定向重测。这里必须记 0.0 并明说, 让看门狗走"无基准"路径(rec=0 时不比比例)
                r["speed"] = 0.0
                r["router"] = "no change (same ip, steady unmeasurable)"
                print(f"  [warn] {name}: {cand} 稳态测不出(快测 {quick.get(cand, 0.0):.1f} 有数), "
                      f"不拿快测顶账, speed 记 0", flush=True)
            r["hotspares"] = (head + steady_top_diverse(results, {cand}, host, url_path, tags,
                                                        HOTSPARE_N - len(head),
                                                        {op_of(tags.get(cand, ""))},
                                                        known={cand: st_self}))[:HOTSPARE_N]
            summary.append(r)
            # 同 IP 也必须落一次盘: 这是唯一一个原来不调 router_apply 的分支, 于是"切了一半"
            # (assets1 已切到 cand、assets2 还钉着死 IP)只会打 [warn], 永远不修 —— 要等某天冠军
            # 真赢 15% 才顺带修好, 而那一天可能永远不来。router_apply 是幂等的: 全部已一致时
            # 它在发任何 ssh 之前就返回 (False, "no change"), 不产生额外流量。
            _ch, _det = router_apply(doms, cand, current)
            if _ch:
                r["router"] = "no change (same ip, residue fixed)"
                print(f"  [router] 同 IP 顺手修残留: {_det}", flush=True)
            elif _det == "no change":
                print(f"  [router] no change (same ip {cand}, steady {st_self:.1f})", flush=True)
            else:
                # 第三种结果(写 uci 失败 / restart 失败)原来被同一行 "no change" 盖掉 ——
                # 也就是 v2.3.15 刚补的那次 router_apply **失败了也看不出来**, 而另外四个分支
                # 一律记 apply FAILED。这条分支曾经因为"不落盘"出过事, 别再让它因为"不记账"出一次。
                r["router"] = "same ip, residue apply FAILED: " + _det
                print(f"  [router] 同 IP 残留修复失败({_det}), 路由器仍不一致, 待下一轮重试", flush=True)
            continue
        # 稳态复测: 冠军候选 vs 当前IP, 赢迟滞阈值才换(摊平突发, 防虚高换节点)
        st_cand = median_steady(cand, host, url_path)
        st_cur = median_steady(cur_ip[name], host, url_path) if cur_ip[name] else 0.0
        # 本轮**已经测过**的稳态值: 一处定义, 下面所有 steady_top_diverse 调用点共用这一个 dict。
        # v2.3.9 修"冠军一轮测两次"时是逐个调用点手写 known={...}, 于是两个分支各漏一个 key:
        # 切换分支漏当前 IP、st_cur==0 分支漏冠军 —— 漏了就是重测 3 轮(0.75GiB), 而且进热备名单的是
        # **第二次**那个抖动值(Cn2 单次差 4 倍), 第二次返回 0 时该成员直接从名单静默消失。
        # 逐个打补丁已经漏过两次(C38b 只查"传了 known=" 这个语法事实, 所以全绿也拦不住),
        # 改为先算一份 dict, 调用点只传变量 —— 结构上不可能漏。0 不进 dict: 0 是"这轮测不出", 留着重测。
        known_measured = {}
        if st_cand > 0:
            known_measured[cand] = st_cand
        if cur_ip[name] and st_cur > 0:
            known_measured[cur_ip[name]] = st_cur
        wr = WIN_RATIO.get(name, 1.15)
        # 当前IP的快测值: 快测能出数就说明链路活着。
        # 稳态 0 有两种含义 —— 节点真死, 或者稳态测量本身失败(2026-11-07: dlassets 对 256MB Range 超时,
        # 快测 90+ 的节点稳态全 0)。只有快测也 0 才算死, 否则按"测量异常"保持原状。
        # 取值必须用 quick 表(含全部被测者)而不是 results(已被 sp>0 过滤): 从 results 取会把
        # "预算跳过没测" 当成 0 分, 与"测了确实 0"混为一谈 => 冤枉一个活节点是死的。
        q_cur = quick.get(cur_ip[name], 0.0) if cur_ip[name] else 0.0
        cur_alive = q_cur > 0
        # 离散度打完之后, 还得回答"这个抖动能不能翻掉结论": 只有横跨阈值才是真由噪声决定
        _note = decision_note(cand, st_cur * wr) if st_cur > 0 else ""
        if _note:
            print(f"  [t] 判定 {cand} vs {cur_ip[name]}(阈值 {st_cur * wr:.1f}): {_note}", flush=True)
        if st_cur > 0 and st_cand < st_cur * wr:
            r["router"] = f"kept (steady {st_cand:.1f} < cur {st_cur:.1f}*{wr})"
            r["ip"] = cur_ip[name]
            r["speed"] = round(st_cur, 1)
            r["hotspares"] = ([{"ip": cur_ip[name], "steady": round(st_cur, 1)}] +
                              steady_top_diverse(results, {cur_ip[name]}, host, url_path, tags,
                                                 HOTSPARE_N - 1, {op_of(tags.get(cur_ip[name], ""))},
                                                 known=known_measured))
            print(f"  [router] 保持 {cur_ip[name]}: 冠军稳态 {st_cand:.1f} < 当前稳态 {st_cur:.1f}*{wr}", flush=True)
        elif st_cur == 0 and not cur_alive and st_cand >= REPLACE_MIN:
            # 当前IP快测也测不出 = 真死, 且替换者达到可用下限 => 切换。
            # 缺了 REPLACE_MIN 这一条就是 2026-11-07 的事故: 冠军稳态 14.1 也照切, 把 93 的节点换掉;
            # 迟滞阈值只适用于两个都健康时的防抖, 对死IP不适用
            changed, detail = router_apply(doms, cand, current)
            if changed:
                r["speed"] = round(st_cand, 1)
                r["router"] = detail + " (cur dead by quick+steady, switched)"
                r["hotspares"] = ([{"ip": cand, "steady": round(st_cand, 1)}] +
                                  steady_top_diverse(results, {cand}, host, url_path, tags,
                                                     HOTSPARE_N - 1, {op_of(tags.get(cand, ""))},
                                                     known=known_measured))
                print(f"  [router] 当前IP快测+稳态均测不出(判死), 切换 {cand} (稳态 {st_cand:.1f})", flush=True)
            else:
                # 写失败 = 路由器仍是那个死 IP: 绝不能记成 cand(否则 results.json 与路由器实况不符,
                # 看门狗拿 cand 当基准会错位, speed.log 也留下"已切换"的假账)
                r["ip"] = cur_ip[name]
                r["speed"] = 0.0
                r["router"] = "apply FAILED (cur dead): " + detail
                r["hotspares"] = steady_top_diverse(
                    results, {x for x in (cand, cur_ip[name]) if x}, host, url_path, tags, HOTSPARE_N,
                    known=known_measured)
                print(f"  [router] 切换 {cand} 写路由器失败({detail}), 记录保持原状待下轮重试", flush=True)
        elif st_cur == 0:
            # 守恒分支, 两种成因要写清楚(别让下一个人以为"测不出=死了"):
            #   a) 当前IP快测还能出数 -> 链路活着, 是稳态测量失败(超大 Range 被 CDN 拒/超时)
            #   b) 当前IP确实死了, 但冠军稳态低于 REPLACE_MIN -> 换过去也是烂节点, 不如等 watchdog 用热备
            if cur_alive:
                r["router"] = f"kept (cur steady unmeasurable, quick {q_cur:.1f} alive)"
                why = f"当前IP稳态测不出但快测 {q_cur:.1f} 仍活(判定为测量异常)"
                # 这里必须和上面 same-ip 分支同一个口径: speed 只放**稳态**, 测不出就记 0.0。
                # 原实现写的是 round(q_cur, 1) —— 那是快测值(80MB 小块), 和稳态差着量级:
                # 2026-11-07 07:59 真机 Cn2 快测 81.7 / 稳态 79.5 还差不多, 但 00:29 那轮快测 93.8、
                # 稳态 3 轮全 0。写进 results.json 后 load_record 会把它当基准(Cn2 的 pinned IP 不在
                # hotspares 里, 只能回退到 speed), 下一小时诚实的 21~40 一律判 degraded
                # (21 < 93.8*0.55=51.6) —— 正是 same-ip 分支那条注释里描述的事故, 换个分支又长回来了。
                # 快测值本身没丢: 它就在 router 字符串里(quick 93.8 alive), 人看得见, 但不当基准。
                r["speed"] = 0.0
            else:
                r["router"] = f"kept (cur dead, cand {st_cand:.1f} < {REPLACE_MIN:.0f})"
                why = f"当前IP已死但冠军稳态 {st_cand:.1f} < {REPLACE_MIN:.0f}, 不换更烂的"
                r["speed"] = 0.0
            r["ip"] = cur_ip[name]
            r["hotspares"] = steady_top_diverse(results, {cur_ip[name]}, host, url_path, tags,
                                                HOTSPARE_N, known=known_measured)
            print(f"  [router] 保持 {cur_ip[name]}: {why}", flush=True)
        else:
            changed, detail = router_apply(doms, cand, current)
            if changed:
                r["speed"] = round(st_cand, 1)
                r["router"] = detail + (" 需Windows复核" if name == "Cn2" else "")
                r["hotspares"] = ([{"ip": cand, "steady": round(st_cand, 1)}] +
                                  steady_top_diverse(results, {cand}, host, url_path, tags,
                                                     HOTSPARE_N - 1, {op_of(tags.get(cand, ""))},
                                                     known=known_measured))
                print(f"  [router] {detail}", flush=True)
            else:
                # 同上: 写失败就按"没切"记账, 保持当前 IP 与它的实测稳态
                r["ip"] = cur_ip[name]
                r["speed"] = round(st_cur, 1)
                r["router"] = "apply FAILED (kept cur): " + detail
                r["hotspares"] = ([{"ip": cur_ip[name], "steady": round(st_cur, 1)}] +
                                  steady_top_diverse(results, {cur_ip[name]}, host, url_path, tags,
                                                     HOTSPARE_N - 1, {op_of(tags.get(cur_ip[name], ""))},
                                                     known=known_measured))
                print(f"  [router] 切换到 {cand} 失败({detail}), 实际仍为 {cur_ip[name]}, 待下轮重试", flush=True)
        summary.append(r)
    elapsed = int(time.time() - t0)
    # 流量账: 2026-11-06 NAS 网卡计数器实测一轮全量收 9.58GiB, 此前没人算过也没人看过。
    # 稳态每次 256MiB × 3 轮 × 每个**实际开测的节点**, 是这轮流量的绝对大头, 必须打在日志里可核对。
    # v2.3.9: 原来打 "× STEADY_CAND_MAX 候选"(= 4×3×256MiB = 3GiB/组), 而真机每组 4.02GiB ——
    # 差额是 steady_top_diverse 之外的 median_steady(冠军/当前IP 各自 3 轮)。打实测节点数才乘得平。
    print(f"[t] 稳态复测累计传输 {_STEADY_TRAFFIC[0] / 1024 ** 3:.2f} GiB"
          f" (实测节点 {_STEADY_NODES[0]} 个 × {STEADY_ROUNDS} 轮 × 单次上限 "
          f"{STEADY_MAX_BYTES // 1048576}MiB)", flush=True)
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
    # 收尾再打一次标记: 冷却要按"上一次重测**测完**"算。只打在开头的话, 一次 3405s 的定向重测
    # 结束那一刻标记已经过期 3405s —— 比整个冷却窗口还长, 下一小时必然放行, 等于没有冷却。
    # 仍是同一个 mark_retest(单写者不变); 中途异常退出时开头那次标记留在原地, 退化成"从开始算"。
    mark_retest()


if __name__ == "__main__":
    main()
