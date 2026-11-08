#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Xbox 加速看门狗 v2.3.36: 每小时体检两组钉住 IP (TCP 连通 + 稳态吞吐),
任一组死掉、吞吐 < 8MiB/s、或掉到记录值一半以下(degraded) =>
  1) 热备止血: 读 results.json 的 hotspares(NAS 上轮全量测速验证过的次优节点),
     逐个用热备自己的测速对实测 8s, 第一个达标的**整组域名一起** uci 切过去 (分钟级恢复, 不等全量重测)
     达标线: 死/极慢 >= 20MiB/s; 只是劣化则还要比当前实测再快 10%(否则越止越慢)
  2) 定向重测: 触发 xbox-speedtest.py --pool <坏组> (只重测坏组, 不再全池 3.2h)
     **带 3 小时冷却**(RETEST_COOLDOWN): 冷却期内只做第 1 步止血, 不重测。
v2.3.36 (2026-11-08 第三十三轮 —— 本文件**一行未改**, 版本号只跟着主程序走):
  - 主程序这轮三条全是**观测/记账面**的账, 与本文件无关, 但有一条值得知道:
    `persist_results()` 新增 `results.json.seen`(写成功才 touch), 用来区分
    `created` 与 `created_after_writefail`。本文件**不读**它, `load_record`/`load_hotspares`
    的读取口径一个未动 —— 只是那个目录里会多一个文件, 别把它当交付物。
  - `.corrupt-<ts>` 隔离文件现在同名冲突时加序号(`-1`/`-2`), 且**永不自动清理**(坏字节的唯一
    证据, 删不删由人定)。本文件从不碰它们。
v2.3.35 (2026-11-08 第三十二轮 —— 本文件**一行未改**, 版本号只跟着主程序走):
  - 主程序这轮修的是 results.json 的**自我维持故障**(坏一次 → 每轮都读到坏文件 → 永不重写),
    把读/合并/隔离/写抽成 `persist_results()`: 坏表先隔离成 `results.json.corrupt-<ts>`,
    再用本轮结果重建。对本文件的意义正是下面 `load_record` 那条注释里的"永久失效"场景 ——
    从此它**最多失效一轮**, 而不是失效到人工发现为止。
  - `load_record`/`load_hotspares` 的读取口径一个未动(仍然是"读不出就打一行日志 + 返回 0.0/[]"),
    所以 A6 之外的断言本文件都不该动; 版本号对齐是 A6 的硬要求。
v2.3.34 (2026-11-08 第三十一轮 —— 外部审查 P1-C / P2-D / P3-F, **本文件三处真改动**, 阈值一个未动):
  - (P1-C) **同一轮两组都止血时, 跨组告警永不触发(实测 0 条)**。`quick_fix(...)` 的返回值被丢掉,
    `pins` 还是本轮开头的值, 于是 Cn2 拿到 `other_pins={Cn1 的旧 IP}` —— 而 Cn1 那个 IP 此刻
    已被换掉, 告警条件"切到了别人正在用的 IP"虽然成立却按旧值判, 两组各切各的互不知情。
    现在 `_r = quick_fix(...)`; 成功就 `pins[name] = _r`。三行, 但少了它这条告警等于没写。
    反证 M110: 把 `pins[name] = _r` 删掉 -> C87 红(实测 warn 条数 0)。
  - (P2-D) **`.last_retest` 没有组维度, 一组的重测压住另一组最长 3 小时**。原来是单个 epoch,
    `--pool Cn1` 一写, Cn2 也跟着进冷却 —— 而 Cn2 那次劣化正是最该立刻重测的。
    现在标记是 JSON `{group: epoch}`, `mark_retest(groups)` 只动传进来的格；
    `retest_cooldown_left(name=None)` 带组读, 不传 name 仍取全组最大值(旧调用点行为不变)。
    **旧格式(裸数字)仍对全部组生效** —— 宁可多压一轮, 也不要在升级瞬间放过本该冷却的那次。
    反证 M111: 冷却不分组 -> C88 红。
  - (P3-F) **`canary()` 的 bool 返回删了**。它是 `state` 的有损副本, 而且没人读:
    `return False` 同时代表 gone / denied / unknown / failed 四种完全不同的事,
    谁拿它当判据都会指错方向。决策早已只走 `state` 字典, 这个返回值是死接口, 删比留更对。
    反证 M113: 把它改回 `return ok` -> C90 红(该断言还查了没有调用点把返回值当值用)。
  - 上一轮(v2.3.33)这里写着"本文件一行未改", 那一轮到这一轮之间**已不成立**, 故改写。
  - 主程序同轮改动见 `xbox-speedtest.py` 版本头: `screen_cap` 拆成 `screen_floor`/`screen_need`、
    `results.json` 读不出不写回、样本补 `t_screen`/`t_quick`。版本号必须跟 A6 对齐。
v2.3.32 (2026-11-08 第二十八轮 —— 外部审查 P2-C, 阈值一个未动):
  - (P2-C) `[span]` 那行把主程序的 `RANGE_SPAN` 抄成**字面量** 160MiB。A23 只比 README ↔ 代码,
    不比**代码 ↔ 代码**, 于是主程序把跨度改成 140/180 时这行照旧念 160 —— 而它存在的全部意义
    就是「让人知道两把尺子不一样」。抄一份这件事本身留下来了(本程序不该为了打一行日志去 import
    主程序: 它崩了看门狗就一起崩), 但漂移现在由审计 **C79** 钉住, 改一处忘另一处会红。
v2.3.31 (2026-11-07 第二十七轮 —— 外部审查 P1-2 / P1-3 / P3-7, 阈值一个未动):
  - (P1-2) **canary 的返回值终于进决策了**。原来 `for _cn in CANARY: canary(_cn)` 把 bool 扔掉,
    于是撤版那天: canary 打一行 404, 接着 `classify(0.0)` 判 dead -> 逐个试热备(每个 256MiB 全 0)
    -> 触发定向重测。每组每小时约 1GiB, 两组约 2GiB, 全打在一个 404 上; 还把钉住的 IP 换到
    一个没验证过的节点。现在 `canary_gate()` 汇总三档:
      404/410          -> **当轮闸门**: 只告警, 不判死、不止血、不重测(直接 return, 省掉 2GiB);
      401/403/429      -> 本轮 0 **不可信**(限流/风控也会让 steady=0): 判 `unmeasured`, 不判死;
      其余(5xx/连不上) -> 不下结论, 体检照做 —— 本机出网断了不该影响"钉住 IP 是否可用"的判断。
    反证 M94: 把 gate 恒返回 "ok" -> C73 红。
  - (P1-3) **Cn2 的相对劣化档实际失效**: `DEGRADE_MIN_RECORD=25` 是照 Cn1 的 100MiB/s 量级定的,
    而 v2.3.8 ⑦ 自己写明 Cn2 测自己那份大包时记录区间是 21~24 —— `rec >= 25` 在那组基本恒假。
    实测 `classify(9.0, 23.9)` 返回 `ok`: 从 24 掉到 9(-62%)判正常。现在门槛分组
    `{'Cn1': 25.0, 'Cn2': 10.0}`, `classify(sp, rec, floor=)` 收组内门槛(默认 25.0,
    旧调用点行为不变)。**Cn1 不动**: 它的记录值本来就在 100 量级。
  - (P3-7) 两个程序测同一个节点用的跨度不同(这里 256MiB, 主程序 160MiB)。**跨度不动** —— 改口径
    在本项目已两次引发生产事故(v2.3.7 把 Cn2 打瞎、v2.3.8 才收回), 而 v2.3.14 已用真机证明
    256MiB 在这条链路上能过。改的是**说法**: 每轮开头打一行 `[span]`, 让"两者不可比"看得见。
v2.3.30 (2026-11-07 第二十六轮 —— **决策逻辑零改动**, 只改 canary 的文案分层):
  canary 原来把 401/403/429/5xx 与 404 报成**逐字相同**的一句话, 全叫人「去换 build」。
  实测(桩 403/429/503)确认四档同文。后果不是漏报, 是**指错方向**: 403 多半是这台出口被限流,
  换了 build 明天照样 403, 然后人会开始怀疑 build 号本身。现在分三档: 404/410 = 撤版(换 build);
  401/403/429 = 被拒(先别换, 连续 3 次同一状态再按撤版处理); 其余 = 不下结论, 看下一小时。
  反证 M91: 把判断改成 `st >= 400` 一律当撤版 -> C71 红。
v2.3.28 (2026-11-07 第二十四轮 —— **有运行行为改动**, 但只在日志面): 新增 `canary()`, 每小时对
  Cn2 的**版本化 URL** 发一个 HEAD, 非 200/206 打一行点名。Cn2 的 TESTPATH 里带着 build 号,
  一撤, 测速与体检用的是同一个 URL —— 同一时刻一起瞎, 日志里只剩 `steady=0.0`, 读起来像"网烂了"。
  canary 买不到"提前 N 天"(CDN 有没有预警期一次都没观测过), 买的是**点名**。
  三条边各有反证: 走 DNS 不绑 IP(M86: 绑 IP 时单台节点坏了会伪装成 URL 下架)/ 只发 HEAD 且 Range
  只要 1 字节 / 200 与 206 都算活(M85: 只认 200 会每小时误报一次)。它不参与任何决策, 只打日志。
v2.3.29 (2026-11-07 第二十五轮): 本文件**零改动**, 只随交付升版本号(A6 要求两脚本版本一致)。
  这轮修的是主程序 `samples.jsonl` 的落盘路径(模块常量 -> 调用时 BASE 拼, 审计跑 main 会污染
  真文件), 与本文件的 canary/体检逻辑无交集。新增的 A26 在本文件上也生效: 审计前后
  `run.log` / `results.json` 必须一字节不动。
v2.3.27 (2026-11-07 第二十三轮): 本文件**零改动**, 只随交付升版本号(A6 要求两脚本版本一致)。
  这轮动的是主程序的**分段计时**(L0 从「粗筛」账里拆出来, 只影响一行日志), 与本文件无交集。
v2.3.26 (2026-11-07 第十九轮): 本文件**零改动**, 只随交付升版本号(A6 要求两脚本版本一致)。
  改的是测速主程序的候选来源(省份标签 -> 实测 RTT 排序), 与本文件无交集。
  这轮动的是主程序的**候选来源**: L0 用 `rtt_rank`(全池 TCP443 握手 min-of-3, 升序取前 RTT_TOP=48)
  替换"省份标签 + autoMMDD"决定谁能进粗筛。池子里的省份标签是人写的、会漏(真机 21 个"标签外省
  但 ≤20ms"的近节点从来没进过粗筛), RTT 是本机实测的, 不会漏, 也不需要人维护。
  看门狗这边零影响 —— 它不粗筛、不选候选, 只体检已钉住的 IP。
v2.3.24 (2026-11-07 第十七轮): 本文件**零改动**, 只随交付升版本号(A6 要求两脚本版本一致)。
  改的全在 tests/*: 台账条目加了"层级"(带日期的嵌套子项既不许假红成"逆序", 也不许灌进条数下限)、
  日期解析接受单位数月/日(2026-11-1 过去整行隐形)、A22 补 PEP 585 内置泛型下标(3.8 求值即 TypeError)。
v2.3.23 (2026-11-07 第十六轮): 本文件**零改动**, 只随交付升版本号(A6 要求两脚本版本一致)。
  改的全在 tests/audit.py: A18 的条目解析不再只认顶格(缩进/星号写法过去是整条跳过的后门)、
  A18 的条数下限 20 -> 50(砍掉历史仍绿)、A19 认动态传入的段兜底编号 C0(过去把真话判成假账)、
  新增 A22(py3.8 语法+API 兼容, 因为 NAS 就是 3.8.10)。
v2.3.22 (2026-11-07 第十五轮): 本文件**零改动**, 只随交付升版本号(A6 要求两脚本版本一致)。
  改的是审计口径: A19 只扫"当前状态区"(README 切在"## 版本历史"之前, 与台账"只扫头部"同构),
  新增 A20(那个边界标题必须存在且唯一) / A21(自检头部变异清单必须与实际一致), 变异 M61~M63。
v2.3.21 (2026-11-07 第十四轮): 本文件**零改动**, 只随交付升版本号(A6 要求两脚本版本一致)。
  真正改的是 tests/audit.py 新增 A19(文档点名的检查编号必须真实存在) + 自检 M60,
  以及台账头部 / README 的措辞 —— 不影响本文件任何一行行为。
v2.3.20 (2026-11-07 第十三轮): 本文件**零改动**, 只更新版本号与下面这条实测结论。
  - 复核"重测超时丢一次体检"这条: 现象成立, 归因不成立。实测(NAS py3.8.10, 复现 main 的顺序 ——
    持 .watchdog.lock -> 拉一个会卡死的子进程 -> RETEST_TIMEOUT 到期 kill -> 捕获 TimeoutExpired):
      子进程被 kill 后 .run.lock 立刻可再取          => True
      父进程仍活着时第二个看门狗实例进不来            => False(这是对的, 它在干活)
      父进程退出后 .watchdog.lock 可再取              => True
    所以**没有锁泄漏**: 丢体检只会发生在"父进程被 5400s 上限挡住、下一小时 :30 撞上合法持锁"那一次,
    最多一次, 且只可能出现在真的卡死(正常 3405s 之内跑完不会跨过 :30)。据此不改 RETEST_TIMEOUT。
  - 相关: 台账(AI共管台账.md)排序已由 tests/audit.py 的 A18 钉住, 与 run.log 无关, 此处记一笔备查。
v2.3.19 (2026-11-07 第十二次真机审计 —— 另一个 AI 复核本仓, 8 条里逐条取证):
  - 修 bug(三态压平, 只修了半边): v2.3.18 给主程序加 `|| true` 时**没给本文件加**, 于是
    get_pins 里 ssh 真挂(=None) 与 "可达但一条 address 都没有"(=空配置) 在日志里是同一句
    `[skip] 路由器不可达或无钉住记录` —— 该去查配置的场景被指向查网络。现在 get_pins 返回
    None 表示 ssh 真挂、返回每组 None 的 dict 表示可达但空配置, main 分两句打。router_apply /
    quick_fix 两处命令串一并对齐(它们本来有 `or ""` 容错, 但第三种口径没必要留着)。
  - 修 bug(degraded 档静默失效): load_record 读不出 results.json 时 `return 0.0`, 而
    classify 里 `rec >= DEGRADE_MIN_RECORD` 对 0 永假 -> 只剩 dead/slow 两个粗档, 而"慢衰减"
    正是相对比值特性的立项理由。全程零日志, 运维看到的"最近很稳"是假的。现在打 [record] 行,
    main 里另补一句"无劣化基准(rec=0) => 本轮只判 dead/slow"。
  - 修 bug(BindIPConn 硬编码 80): 改成 `self.port or 80`。现在 HTTPConnection 默认端口也是 80
    所以无害, 但哪天改 HTTPS/8443, Host 头仍是域名、CDN 照回 200, 测的是错端口的流量且不报错。
  - 删死常量 TEST_HOST / TEST_PATH(v2.3.8 起 steady 不给默认值, 它们只剩 docstring 里被提到;
    A8 只查函数不查模块级常量, 所以一直绿)。
  - **未动**: 三个 conn 在异常路径不 close(CPython 引用计数会收, 真机长跑实测无 fd 增长)、
    record_pin 的固定 .tmp 名(.run.lock 已串行化)—— 两条都已在 README 挂账。
v2.3.18 (2026-11-07 第十一次真机审计 —— 复核另一 AI 对旧副本的 13 条):
  - 修 bug(触发是"盲的"): `subprocess.run(...)` 原来既不设 timeout 也不看 returncode, 日志只有
    [trigger] 没有结果 —— 子进程被 OOM/异常杀掉、或抢锁失败早退(退出码 0)和一个真的跑完的重测
    长得一模一样。定向重测是本程序唯一花钱的动作(单组 3.71GiB), 它的结果必须有据可查。
    现在带 RETEST_TIMEOUT=5400 并在下一行打退出码(非 0 时明说"查 speed.log 是否多了一行")。
  - 同一份审计里"锁不同名"那条是 v2.3.15 已修的内容(当时审计的是 10 月的旧 watchdog.py):
    现本文件与主程序共用 .run.lock, .watchdog.lock 只防自己重叠, 两者分工见 run_lock_taken 注释。
  - 未动的部分: THRESHOLD_MIBS / DEGRADE_RATIO / HOTSPARE_MIN_MIBS / STEADY_MAX_BYTES 全部原样。
v2.3.17 (2026-11-07 第十次真机审计, 两条都是"注释声称的效果其实没兑现"):
  - 修 bug(体检口径的假话): 注2 原来写"健康体检统一用 Cn1 测速文件, 因此不依赖 Cn2 版本化 URL"。
    那是 v2.3.8 之前的实现, 现在的 main 与 quick_fix 一律传 DOMAIN[name]/TESTPATH[name](本组测速对)。
    更糟的是这句话把**爆炸半径说小了**: 它暗示"版本化 URL 下架 = speedtest 瞎、watchdog 免疫",
    实际是两侧 Cn2 同时瞎 —— 止血验证用的是同一个死 URL, 一个热备都验不出来。已改文案(注2)。
  - 修 bug(基准意图只送到一半): record_pin 注释说"speed 写实测值, 免得基准虚高", 但 load_record
    的优先级是"先在 hotspares 里找 pinned IP 的旧 steady" —— 而止血切到的 IP 必然来自 hotspares,
    所以写进 grp.speed 的实测值**根本到不了基准路径**, 下次体检用的还是 04:00 那次全量的旧值
    (真机 100.3 写进去, 基准读 105.4)。现在 record_pin 顺带把 hotspares 里该 IP 的 steady 一起更新
    (仍是同一个原子写), 注释与行为对齐; C58 用 load_record 的返回值断言(不是查字段, 查字段查不出这条)。
v2.3.16 (2026-11-07 第九次真机审计, 专审上一轮新加的三行自己带出来的账):
  - 修 bug(止血成功不记账): 止血把钉住 IP 换掉后, 本脚本**不碰 results.json**, 于是从那一刻起
    记录写的是死 IP、路由器钉的是新 IP, 一直到明天 04:00 的全量才对齐(最长 19.5 小时)。
    2026-11-07 09:30 真机就是这个现场: 09:30:26 切到 14.29.101.160, 09:30:56 的 speed.log
    里 Cn2 还是 58.251.60.41 —— 当时归因于并发, 但即使没有并发, 记录同样不会变。
    这正是 v2.3.5 ④ 和 v2.3.15 ④ 修过两次的"记账与路由器实况不符", 这次从"看门狗不写账"长回来。
    现在止血成功后、**仍在 .run.lock 内**用 record_pin() 原子回写该组的 ip/speed/router
    (热备名单不动), 并把测速实测的 100.3 写进 speed —— 不写记录里的旧值, 否则基准虚高又变回
    "每小时 degraded"(v2.3.6/v2.3.12 各修过一次的那条)。
  - 修 bug(锁建不了 = 测速在跑): run_lock_taken() 把"打不开 .run.lock"(权限/磁盘满)和
    "被另一个进程占着"都返回 False, main 一律打印"测速主程序正在跑"。真出现写权限问题时,
    止血会**永久**跳过, 而日志每小时指着一个不存在的并发进程 —— 本文件 main() 对
    .watchdog.lock 的 ImportError 特意分了两条路, 这里却在同一处把两件事压成一件。
    现返回第三种结果 LOCK_ERR, 打专门的日志("不动路由器, 别去查并发")。
  - 加告警(热备跨组重复): 2026-11-07 09:37 的 results.json 里 Cn2 的 hotspares[1] 是
    125.88.64.72 —— 那正是 Cn1 钉着的 IP。名单按运营商分散, 却没按"另一组钉着的 IP"去重,
    单节点故障会同时打掉两队: Cn2 的独立备胎实际只剩 1 个。止血前会实测所以不会切到死的,
    本次只加告警(选择策略属阈值变更, 不在本轮动)。
v2.3.15 (2026-11-07 第八次真机审计 —— 修的是 v2.3.14 自己那个补丁):
  - 修 bug(冷却永远不命中): v2.3.14 设成 45 分钟, 而本脚本的 cron 是**每小时** :30 一次,
    两次触发相隔 3600s > 2700s, 冷却**一次都不可能命中**。NAS 上用部署中的代码现算:
    标记 age=3600 -> retest_cooldown_left()=0 -> 照常重测。也就是说它声称拦住的
    2026-10-27 那串 20:30:26 / 21:30:35 / 22:30:32 / 23:30:16(间隔 3609/3597/3584s)
    在 45 分钟冷却下会**一模一样地重演** —— 这是"补丁写了、审计全绿、问题还在"的又一例:
    C48 桩测用的是 "1 分钟前刚测过"(当然能命中), 没有任何断言问过
    "这个值跨不跨得过触发周期"。现改为 3 小时, 并加 C50 钉住
    "RETEST_COOLDOWN 必须 > 2 个 cron 槽位", 同时把 README 里的 cron 行一起钉住。
  - 相关(不在本文件): .last_retest 现在在测速**正常结束**时再写一次, 冷却从"测完"算起。
    只打在开头的话, 一次 3405s 的长跑结束时就已过期, 3 小时窗口被吃掉 32%。
  - 修 bug(两个进程写同一个路由器, 各拿一把锁): 本脚本一直只锁 .watchdog.lock, 而测速主程序锁
    .run.lock —— 等于没锁。2026-11-07 09:30 真机逮到现场: 09:28 的手动全量跑到 09:30:56,
    09:30 的看门狗在这中间把 Cn2 切到 14.29.101.160(那一刻 31.0MiB/s => degraded),
    全量却在 09:30:56 按开始时读到的 uci 记 "kept 58.251.60.41 84.9"。
    两个进程的 uci 快照相差 2 分钟, 后写的赢, 而 results.json 只描述自己那份快照 ——
    这正是 v2.3.5 ④ 修过的"记账与路由器实况不符", 换了个来源(并发)又长回来。
    现在止血前先拿 .run.lock(与主程序同一把), 拿不到就整轮跳过(也不重测), 下一小时再来。
  - 未动的部分: THRESHOLD_MIBS / DEGRADE_RATIO / HOTSPARE_MIN_MIBS / STEADY_MAX_BYTES(256MiB) 全部原样。
    特别说明 256MiB: 外部审计判定它是"地雷", 本机实测**不复现**(211.97.83.41: 256MiB 100.9 / 160MiB 91.8
    MiB/s, 512MiB 跨度也返 206), 因此没以"拆雷"为理由改它; 若要改, 理由是流量(2 组×256MiB×24h=12GiB/天
    -> 7.5GiB/天), 属另一件事, 需单独一轮真机验证后再动。
v2.3.14 (2026-11-07, 按外部审计复核结果落的补丁 —— 本脚本第一次真的改逻辑):
  - 加冷却(重测无退避): 这原先是个**长期存在的结构性浪费**, 不是理论风险 —— 2026-10-27 真机
    20/21/22/23 连续四小时各触发一次全量(3405s/2069s/1284s/1045s; 20:30 那轮跑到 21:27:12,
    21:30:35 又开)。单组实测 3.71GiB -> 组整夜死着时 24 次/天 ≈ 89GiB, 且大概率测完还是老样子。
    标记文件 .last_retest 由 xbox-speedtest.py 在拿锁后写(单写者), 本脚本 retest_cooldown_left() 只读。
    未来时间戳(时钟回拨/误写)按"没见过"处理, 否则一次误写能把重测冻到任意久。
    **注: 当时的 45 分钟在一小时 cron 下从未命中, 见 v2.3.15。**
  - 未动的部分: THRESHOLD_MIBS / DEGRADE_RATIO / HOTSPARE_MIN_MIBS / STEADY_MAX_BYTES(256MiB) 全部原样。
    特别说明 256MiB: 外部审计判定它是"地雷", 本机实测**不复现**(211.97.83.41: 256MiB 100.9 / 160MiB 91.8
    MiB/s, 512MiB 跨度也返 206), 因此没以"拆雷"为理由改它; 若要改, 理由是流量(2 组×256MiB×24h=12GiB/天
    -> 7.5GiB/天), 属另一件事, 需单独一轮真机验证后再动。
v2.3.13 (2026-11-07 第七次真机审计): 本脚本逻辑未改, 只随主程序对齐版本号。
    吸收的是主程序的记账缺陷: 热备名单里成员的 steady 值原来可能来自**第二次**测量(同一 IP 在一轮里
    被稳态测两遍), 抖动到 0 时该成员会从名单里静默消失 —— 本脚本按"名单短就是真的没有可用热备"
    处理, 于是止血时少一张牌, 日志看不出原因。主程序已改为每个 IP 一轮只测一次且用决策那次的值。
v2.3.12 (2026-11-07 第六次真机审计): 本脚本逻辑未改, 只随主程序对齐版本号。
    相关但不在本文件: 主程序把"稳态测不出"两条路的 speed 记账统一成 0.0 —— 原来 alive 那条写的是
    快测值, 于是本文件的 load_record 会拿 93.8(快测) 当基准, 把诚实的 21~40 判成 degraded。
    另: HOTSPARE_MIN_MIBS 与主程序的 REPLACE_MIN、steady_top_diverse(min_steady=) 现在是三份
    同值的拷贝, 由审计 C44 钉死必须相等 —— 改任何一份都会被审计打红。
    相关但不在本文件: 主程序新增 decision_note(), 把"3 轮离散度"与"抖动能否翻掉结论"分开判;
    还查明 median_of 会滤掉 0 值 —— 0/0/100 的"中位"是 100.0, 也就是三轮里两轮测不出仍记满分。
    (以上两条是 v2.3.11 的记录, 保留供追溯)
    本脚本的 degraded 判定用的是**相对记录值的比例**(rec*0.55), 同样受 Cn2 的 4 倍抖动支配,
    而 steady() 是**单次采样**, 连离散度都看不见, 更分不出"这一轮是失败还是真慢"。
    因此这里**没有**加类似逻辑(单样本算不出区间), 只在主程序侧留证据。
    真要让 degraded 可信, 先得有基准值的分布, 不是再调一个系数
v2.3.9 (2026-11-07 第四次真机审计): 本脚本逻辑未改, 只随主程序对齐版本号。
    主程序那条"冠军一轮被测两次"的账也影响本脚本的输入: results.json 里 hotspares 的冷门成员
    曾经可能是**第二次测量**的值(Cn2 抖动 4 倍, 第二次 0 时整个成员会消失)。本脚本读名单时
    按"名单短就是真的没有可用热备"处理, 不补测 —— 因此名单质量只能靠主程序那边保证
v2.3.8 (2026-11-07 第三次真机审计): 本脚本逻辑未改, 只随主程序对齐版本号。
    相关但不在本文件: 主程序把 RANGE_SPAN 从 256MiB 退回 160MiB —— 真机 dlassets 对大跨度 12s 超时,
    会让稳态全返回 0 而被读成"节点已死"。watchdog 的 steady() 一直用 STEADY_MAX_BYTES 直接当跨度,
    对 Cn2 是同一条地雷: 若哪天 dlassets 连 256MB 都拒, 体检会集体报死并触发止血乱切 ——
    目前真机测量 256MB 跨度在 watchdog 侧尚未复现超时(它是新建连接单发请求, 与续测不同),
    所以**没有**跟着改成 160MiB, 但这是已知风险, 见 README 已知局限
v2.3.7 (2026-11-06 第二次真机审计):
  - 修 bug(口径不一致): steady() 的 Range 写 1GB、注释却称"单次约 160MB 不续测", 两头都错。
    真机 HEAD: Cn1 测速文件 295.4MiB(读完即止, 实测收 309MB), Cn2 是 40.4GiB 包(8s 能跑 700MB+)。
    结果同一节点 watchdog 报 101.4 / speedtest 报 96.4, 5% 系统偏差让 DEGRADE_RATIO 判定偏乐观。
    现统一 STEADY_MAX_BYTES=256MiB, 两边窗口同量级、记录值可直接比对; 每小时流量 0.6GB → 0.5GB
v2.3.6 (2026-11-06 NAS 实跑复盘): 本脚本逻辑未改; 同步了版本号与 README 口径(见 xbox-speedtest.py 同版本)
v2.3.5 (2026-11-06 真机数据审计):
  - 修 bug(劣化基准错位): load_record 只认"上轮冠军的稳态", 不看路由器此刻实际钉的是谁。
    止血切到热备 84MiB/s 后, 基准仍是旧冠军 108 -> 实测 56 就被判 degraded(56 < 108*0.55),
    每小时反复触发定向重测。改为优先取 hotspares 中**当前钉住 IP** 那条的稳态, 找不到才回退旧口径
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
DEGRADE_MIN_RECORD = {"Cn1": 25.0, "Cn2": 10.0}   # 记录值本身不到这个数不做相对判定(本就不快的节点别抖)
DEGRADE_MIN_RECORD_DEFAULT = 25.0                 # 未登记的组按 Cn1 量级处理(保守 = 少报)
# v2.3.31: 分组。原来一个 25.0 通吃, 那是照 Cn1 的 100MiB/s 量级定的; 而 v2.3.8 ⑦ 自己写着
# "Cn1 文件 98~105、Cn2 自己的包 21~24", 也就是说 Cn2 的记录值**基本恒 < 25** ->
# `rec >= DEGRADE_MIN_RECORD` 在那组恒假 -> degraded 档永不触发, 只剩 8MiB/s 绝对线。
# 实测取证: classify(9.0, 23.9) 返回 ('ok','ok') —— 从 24 掉到 9(-62%)判"稳定"。
# 10.0 是从 Cn2 的记录区间(14.1/20.6/21.3/23.9)下沿取的: 放得进"这组能到 21~24"的事实,
# 又不会让"本来就 12 的节点"参与相对判定。Cn1 的 25.0 **不动**(它的记录值在 100 量级)。
HOTSPARE_MIN_MIBS = 20.0      # 止血门槛: 热备实测 >= 此值才切(且必须显著高于 THRESHOLD)
DL_TIMEOUT = 12.0
STEADY = 8.0
STEADY_MAX_BYTES = 256 * 1024 * 1024   # 必须与 xbox-speedtest.py 的 STEADY_MAX_BYTES 一致:
                                       # 记录值(hotspares.steady)与实测值窗口不同量级时, 劣化判定会系统性偏移
# 定向重测冷却: 上次重测不到这个时长就只止血不重测。
# 没有它, 某组整夜死着(整区段故障/热备全灭)时, 每小时 :30 都会触发一次定向重测:
# 2026-10-27 真机就发生过 —— 20/21/22/23 连续四小时各触发一次全量(3405s/2069s/1284s/1045s)。
# 成本按本程序自己的账核: 单组实测 3.71GiB(实测节点 5 个) -> 24 次/天 ≈ 89GiB。
# **必须跨过多个 cron 槽位**: 本脚本 cron 是每小时一次(README 部署第 4 步), 所以冷却 <= 60 分钟
# 等于没有 —— 两次触发相隔 3600s, 冷却早过期了。v2.3.14 的 45 分钟就是这样: 桩测(C48)用
# "1 分钟前刚测过"当然命中, 真机 cron 下却一次都不命中, 2026-10-27 那串会逐字重演。
# 现取 3 小时: 一夜(23:00~07:00)最多 3 次, 89GiB -> 约 30GiB。C50 钉住这条前提。
RETEST_COOLDOWN = 3 * 3600
# 定向重测子进程的墙钟上限: 真机单次最长 3405s(56 分钟), 取 5400s(90 分钟) 留余量。
# 没有它 + 不看 returncode = 日志里只有 [trigger] 没有结果, "被 OOM 杀掉"与"跑完了"无法区分。
RETEST_TIMEOUT = 5400
RETEST_MARK_NAME = ".last_retest"   # 与 xbox-speedtest.py 同名; 路径在调用时用 BASE 拼(测试要能重定向)

# run_lock_taken() 的第三种结果: 锁文件**建不了**(权限/磁盘满/目录只读)。
# 不能和"被占着"共用 False: 那会让 main 打印"测速主程序正在跑", 而真相是止血从此永久跳过,
# 日志每小时指着一个不存在的并发进程。字符串常量故意不用 FAIL_CONSTS 里的值(B1 棘轮口径)。
LOCK_ERR = "lock-unopenable"

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
# 注2: 健康体检与止血验证**都用本组自己的测速对**(v2.3.8 起; Cn2 用 dlassets + 版本化安装包 URL)。
#      历史上这里写的是"体检统一走 Cn1 文件, 因此不受 Cn2 版本化 URL 下架影响" —— 那是 v2.3.8 之前的
#      行为, 早已失效, 而且把爆炸半径说小了: 版本化 URL 是 Cn2 测量的**单点**。微软下架那个版本时,
#      不是"speedtest 失效而 watchdog 有免疫力", 而是两侧 Cn2 同时瞎掉 —— 止血验证用的是同一个死 URL,
#      一个热备都验不出来(于是不是"止血慢", 是"止血彻底做不了")。这条仍挂账, 见 README 已知局限。
# 注3: 热备止血验证也必须用热备自己的测速对(Cn2 热备是 dlassets 边缘, speedtest 用 dlassets+版本化URL
#      实测入围)——拿 assets1 内容去验会误切"assets1 快但 dlassets 慢"的节点


class BindIPConn(http.client.HTTPConnection):
    def __init__(self, host, ip, timeout):
        super().__init__(host, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port or 80), timeout=self.timeout)


def ts():
    return time.strftime("%F %T")


def router_cmd(cmd):
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
                            "-i", SSH_KEY, f"root@{ROUTER}", cmd],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=30)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception as e:
        print(f"[router] ssh 失败: {e}", flush=True)
        return None


def parse_hist(current):
    """uci show 的 address 行 -> {domain: [ip, ...]}, **保留写入顺序**。
    v2.3.7: 原实现返回 set, get_pins 再 sorted(ips)[0] 取字典序最小的 IP —— 与 xbox-speedtest.py
    那份是同一条缺陷的两个实现(上次修主程序时漏了这里)。多值残留时会拿一个早该废弃的 IP 当"当前钉住",
    进而让止血去切一个路由器上早已不用的节点。"""
    hist = {}
    for m in re.finditer(r"'/([^/]+)/([0-9.]+)'", current):
        lst = hist.setdefault(m.group(1), [])
        if m.group(2) not in lst:
            lst.append(m.group(2))
    return hist


def get_pins():
    """从路由器 uci 读两组首域名的钉住 IP(list 末尾 = 最近 add_list 的那个)。

    v2.3.19: 返回 **None 表示 ssh 真不可达**, 返回 `{}` 表示"可达但一条 address 都没有"。
    这两种状态原来都是 `{}`, main 里压成同一句 `[skip] 路由器不可达或无钉住记录` —— 与
    v2.3.18 在主程序里修的是同一个洞, 当时只修了一半(主程序 xbox-speedtest.py:635 加了
    `|| true`, 这里没加), 于是"路由器配置被 del_list 清空 / 换新路由器"这种**该去查配置**的
    场景, 日志指向的是**该去查网络**。命令串必须带 `|| true`: `grep address` 无匹配行时退 1,
    router_cmd 把非 0 折成 None(2026-11-07 本机复现: printf "" | grep address -> rc=1)。"""
    current = router_cmd("uci show dhcp | grep address || true")
    if current is None:
        return None
    pins = {}
    hist = parse_hist(current)
    for name, domain in DOMAIN.items():
        ips = hist.get(domain)
        pins[name] = ips[-1] if ips else None
    return pins


def targets_for(name, old_ip, hist):
    """本组要一起切的域名: 显式表(DOMAINS) + 当前仍指向死 IP 的其他钉住域名。
    后者防配置漂移: 若 speedtest 哪天多钉了域名, 这里也能一起切走, 不留半死状态。
    old_ip 为 None(无钉住)时只用显式表。"""
    doms = list(DOMAINS.get(name) or (DOMAIN[name],))
    if old_ip:
        for d, ips in (hist or {}).items():
            if d not in doms and set(ips or []) == {old_ip}:
                doms.append(d)
    return doms


def router_apply(domains, ip):
    """幂等更新一组域名: 删各自的全部历史值再写新值(防 address 堆积), 全部命令一次 ssh 调用。
    返回 True 才算真的生效(commit + dnsmasq restart 都成功); 只 commit 不 restart 时
    路由器仍返回旧记录, 视作失败, 交由下一个热备/定向重测处理。"""
    current = router_cmd("uci show dhcp | grep address || true")   # v2.3.19: 与主程序同一口径,
    hist = parse_hist(current or "")                                    # None 从此只代表 ssh 真挂
    cmds = []
    for d in domains:
        olds = hist.get(d, [])
        if set(olds) == {ip}:
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


def steady(ip, host, path):
    """绑定 IP 下载, 受「8 秒」与 STEADY_MAX_BYTES 两条上限约束, 返回 MiB/s (0=失败)。
    v2.3.7 校正口径(2026-11-06 NAS 真机): 原 Range 写 1GB 且注释称"单次约 160MB 不续测", 两头都错 ——
    真机 HEAD 实测 Cn1 测速文件 extraextralarge.txt = 309714493B(295.4MiB), 一次读完就 EOF, 实测收到 309MB;
    Cn2 是 40.4GiB 的版本化包, 8 秒能跑 700MB+。现统一用 256MiB 字节上限, 与 speedtest 的窗口同量级,
    hotspares.steady 才能直接拿来当劣化基准比对; 每小时流量 = 2 组 × 256MiB ≈ 0.5GB。
    (曾按单次对比记成"5% 系统性偏差", 但交替测 4 轮均值差只有 0.8%、而单列抖动本身 22% ——
     按「口径统一」记账, 不按「修正偏差」记账。)
    v2.3.8: host/path **不再给默认值**。原来默认 Cn1 的 TEST_HOST/TEST_PATH, 而 main 里两处
    `steady(ip)` 没传 —— Cn2 的每小时体检实际一直在测 Cn1 的文件(assets1 的 295MiB 小文件),
    测出来的数拿去和 Cn2 大包(40.4GiB)实测的记录比, 口径根本不同: 2026-11-07 实测同一时刻
    Cn2 用 Cn1 文件测 98~105, 用自己文件测 21~24, 差 5 倍。degraded 因此永不触发。"""
    try:
        conn = BindIPConn(host, ip, DL_TIMEOUT)
        conn.request("GET", path, headers={
            "Host": host, "Range": "bytes=0-%d" % (STEADY_MAX_BYTES - 1),
            "User-Agent": "XboxDownload", "Accept-Encoding": "identity"})
        resp = conn.getresponse()
        if resp.status not in (200, 206):
            resp.close()
            return 0.0
        t0 = time.perf_counter()
        got = 0
        while time.perf_counter() - t0 < STEADY and got < STEADY_MAX_BYTES:
            b = resp.read(65536)
            if not b:
                break
            got += len(b)
        el = time.perf_counter() - t0
        resp.close()
        return got / (1048576 * max(el, 0.1))
    except Exception:
        return 0.0


CANARY = ("Cn2",)   # 只对**版本化 URL** 做体检。Cn1 的 /Z/routing/extraextralarge.txt 是静态路径,
                    # 不会随某个 build 下架; Cn2 的 path 里带着 `1.5.0.1018.88cd...` 那串 build。


def canary(name, state=None):
    """版本化 URL 体检: 一个 HEAD 回答"那串 build 还在不在"。

    为什么要有这条: Cn2 的 TESTPATH 里带着 build 号, 一撤, **测速和看门狗用的是同一个 URL**,
    于是体检/止血/重测在同一时刻一起瞎 —— 日志里只剩 steady=0.0, 读起来像"网烂了",
    实际是"我们去下载的那个文件没了"。canary 买不到"提前 N 天"(CDN 有没有预警期我们一次都没
    观测过, 可能当天就 404), 它买的是**点名**: `[canary] Cn2 HTTP 404` 比 `steady=0.0`
    少掉三小时的"是不是我的网/路由器"。

    两条硬约束, 各有反证(M83/M84):
    - **必须走 DNS, 不能绑 IP**。绑 IP 时"这台节点坏了"会伪装成"URL 下架", 假警报比不报更有害
      (叫狼来了的次数多了, 真下架那天没人看)。所以这里用明文 HTTPConnection, 不用 BindIPConn。
    - 只发 HEAD 且 Range 只要 1 字节: 每小时一次, 代价几百字节, 不碰 40GiB 的包体。
    - **三种"不 OK"必须长得不一样**: 404/410 才是"文件没了"; 401/403/429 是被拒(限流/风控/
      撤版预警), 照着"换 build"动手就白忙一场; 5xx 偶发更是常态, 不许下任何结论。
    - v2.3.31: 返回值不再只是给人看的。`state` 是回执字典, 写 `ok/gone/denied/unknown/failed`
      五档, 由 `canary_gate()` 汇总成当轮闸门 —— 原来 main 里 `canary(_cn)` 的 bool 被直接扔掉,
      于是"文件没了"和"节点死了"在下面那条 8s 体检里长得一模一样, 一路止到热备去。
    """
    try:
        # 必须是 **HTTP** 不是 HTTPS: 测速与体检走的就是 http.client.HTTPConnection + Host 头
        # (BindIPConn 的基类), 从没做过 TLS 校验。2026-11-07 18:46 在 NAS 上实测: 换 HTTPS 后
        # 真 URL 直接 SSLCertVerificationError(Hostname mismatch) —— canary 每小时报警, 而下载
        # 一直好好的。用 HTTPS 等于给"体检"加了一道被体检对象不遵守的门。
        conn = http.client.HTTPConnection(DOMAIN[name], timeout=DL_TIMEOUT)
        conn.request("HEAD", TESTPATH[name], headers={
            "Host": DOMAIN[name], "Range": "bytes=0-0", "User-Agent": "XboxDownload"})
        resp = conn.getresponse()
        st = resp.status
        resp.close()
        conn.close()
    except Exception as e:
        print(f"{ts()} [canary] {name} HEAD 失败({e.__class__.__name__}: {e}) —— 可能 URL 已失效, "
              f"也可能是本机出网断了; 先看这条再怀疑别的", flush=True)
        if state is not None:
            state[name] = "failed"     # 连不上 ≠ 撤版: 这条**不许**触发闸门, 见 canary_gate
        return
    ok = st in (200, 206)
    if not ok:
        # 三档必须分开, 否则运维照着错的方向动手。2026-11-07 实测: 403/429/503 与 404 打出来的
        # 是**逐字相同**的一句话, 全叫人「去换 build」—— 而 403 很可能只是这台出口被限流,
        # 换了 build 明天照样 403, 然后人会开始怀疑 build 号本身。
        if st in (404, 410):
            if state is not None:
                state[name] = "gone"
            print(f"{ts()} [canary] {name} HTTP {st} {DOMAIN[name]}{TESTPATH[name][:64]}... "
                  f"—— 版本化 URL 疑似撤版: 测速与体检用的是同一个 URL, 今晚 04:00 那轮大概率全 0, "
                  f"去换 build 而不是调阈值", flush=True)
        elif st in (401, 403, 429):
            if state is not None:
                state[name] = "denied"
            print(f"{ts()} [canary] {name} HTTP {st} {DOMAIN[name]}{TESTPATH[name][:64]}... "
                  f"—— 被拒, 不是不存在: 可能出口 IP 被限流/风控, 也可能正处在撤版预警期"
                  f"(有没有预警期一次都没观测过)。先别换 build —— 连续 3 次同一状态再按撤版处理, "
                  f"中间出现 200/206 就作废", flush=True)
        else:
            if state is not None:
                state[name] = "unknown"
            print(f"{ts()} [canary] {name} HTTP {st} {DOMAIN[name]}{TESTPATH[name][:64]}... "
                  f"—— 服务端这次没给内容。这不是「文件不存在」, 不下结论: 先看下一小时"
                  f"是不是同一个状态(5xx 偶发几次很正常)", flush=True)
    elif state is not None:
        state[name] = "ok"
    # v2.3.34 (审查方 P3-F): **不再返回 bool**。原来 `return ok` / `return False` 是 state 的
    # 有损副本 —— False 同时代表 gone / denied / unknown / failed 四件完全不同的事, 而决策
    # 早已只走 `state`(canary_gate 汇总), 于是这个返回值变成了"看着像判据、实际会骗人"的死接口:
    # 下一个人写 `if not canary(x):` 就等于把"404 撤版"和"本机断网"当成一件事 ——
    # 正是上面那段三档注释花力气去防的错。删掉它, 让唯一的判断口只剩 state。


def canary_gate(names=None):
    """把每组的 canary 档位收成一个**当轮闸门**, 返回 (gate, 明细)。

    gate:
      "gone"     —— 有版本化 URL 返回 404/410。这一轮**不判死、不止血、不重测**: 0.0 不是节点的问题,
                    拿它去止血等于把钉住的 IP 换成一个没验证过的节点, 还每小时白烧约 2GiB。
      "denied"   —— 401/403/429。0 **不可信**(限流/风控同样让 steady=0), 判 `unmeasured` 而不是 dead。
      "unknown"  —— 5xx。不下结论, 体检照做(5xx 偶发不能停掉整个看门狗)。
      "ok"/"failed" —— 都按正常体检走。`failed` (HEAD 连不上) 尤其**不能**当撤版:
                    本机出网断了的时候, "钉住 IP 是否可用"仍然值得测。
    优先级 gone > denied > unknown: 一条 404 就能解释整轮的 0, 别的档位都是次要信息。
    """
    st = {}
    for name in (names or CANARY):
        canary(name, state=st)
    for want in ("gone", "denied", "unknown"):
        if want in st.values():
            return want, st
    return "ok", st


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


def load_record(name, pinned_ip=None):
    """读劣化基准: 优先取"路由器此刻真的钉着的那台"在 hotspares 里的稳态,
    否则回退到"上轮冠军(hotspares[0].ip==grp.ip)"的稳态, 再否则用 speed。
    2026-11-06 审计: 止血把钉住 IP 换成热备(84)后, 基准仍是旧冠军(108),
    实测 56 就被判 degraded(56 < 108*0.55) -> 每小时反复触发定向重测。基准必须跟着 IP 走。"""
    try:
        data = json.load(open(os.path.join(BASE, "results.json"), encoding="utf-8"))
    except Exception as e:
        # v2.3.19: 不能静默返回 0.0。`classify` 里 `rec >= DEGRADE_MIN_RECORD` 对 0 永假,
        # 于是 results.json 缺失/损坏/被 --pool 写坏时 **degraded 档永久失效**, 只剩 dead/slow
        # 两个粗档 —— 而"慢衰减"正是 README 里相对比值特性的立项理由。全程零日志时运维只会
        # 看到"最近很稳", 实际是基准没了(2026-11-07 外部审计 P1-4)。
        print(f"{ts()} [record] results.json 读不出({e.__class__.__name__}: {e}); "
              f"本轮没有劣化基准, 只判 dead/slow, degraded 档不参与", flush=True)
        return 0.0
    for grp in data:
        if grp.get("group") != name:
            continue
        hs = grp.get("hotspares") or []
        if pinned_ip:
            for h in hs:
                if h.get("ip") == pinned_ip and h.get("steady"):
                    try:
                        return float(h["steady"])
                    except (TypeError, ValueError):
                        break
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


def record_pin(name, ip, sp, old_ip=None):
    """止血成功后把 results.json 里该组的 ip/speed/router 改成**路由器实况**。

    为什么必须写: 本脚本换掉了钉住 IP, 却原来不碰 results.json —— 于是从那一刻起记录写的是死 IP,
    路由器钉的是新 IP, 要等到明天 04:00 的全量才对齐(最长 19.5 小时)。人看的是 results.json/speed.log
    这份账, 它写"58.251.60.41"而路由器写"14.29.101.160", 与 v2.3.5 ④、v2.3.15 ④ 修过两次的
    "记账与路由器实况不符"是同一件事, 这次从"看门狗不写账"这条路径长回来。
    speed 用**这一刀的实测值 sp**(不是热备名单里的旧记录): 记旧值会让基准虚高, 下一小时诚实的
    低值就判 degraded —— v2.3.6 与 v2.3.12 各修过一次的那条。
    **光写 grp.speed 不够**: load_record 的优先级是"先在 hotspares 里找 pinned IP 的旧 steady",
    而止血切到的 IP 必然在 hotspares 里 —— 实测值到不了基准路径(v2.3.17 实测: 写 100.3, 基准读 105.4)。
    所以顺带把 hotspares 里该 IP 的 steady 也更新成同一刀的值。名单成员与顺序一律不动(那是"下一刀
    可用的备胎", 与"这台现在多快"是两件事), 只改那一格的数。
    只能在 main 持有 .run.lock 时调用(与测速主程序的写不并发), 原子写(os.replace)防半截文件。
    返回 True = 已回写; False = 读不出/没有该组/写失败(下一轮全量会重新对齐, 不影响止血本身)。
    """
    p = os.path.join(BASE, "results.json")
    try:
        data = json.load(open(p, encoding="utf-8"))
        if not isinstance(data, list):
            return False
        hit = False
        for grp in data:
            if grp.get("group") == name:
                grp["ip"] = ip
                grp["speed"] = round(float(sp), 1)
                grp["router"] = "watchdog 止血 switched: %s -> %s" % (old_ip or "?", ip)
                for h in (grp.get("hotspares") or []):
                    if isinstance(h, dict) and h.get("ip") == ip:
                        h["steady"] = round(float(sp), 1)   # 基准跟着 IP 走, 否则这行白写
                hit = True
        if not hit:
            return False          # 该组还不在 results.json 里(首次部署/被清过): 不凭空造条目
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
        return True
    except Exception as e:
        print(f"{ts()} [record] 回写 results.json 失败({e}); 止血已生效, 账等下一轮全量对齐", flush=True)
        return False


def quick_fix(name, pinned_ip, floor_mibs=HOTSPARE_MIN_MIBS, other_pins=()):
    """热备止血: hotspares 逐个实测, 第一个达标的整组切过去, 返回切到的 IP 或 None。
    floor_mibs: 达标线。degraded 场景要传"当前实测×1.1", 否则会把 44MiB/s 的节点换成 30MiB/s 的(越止越慢)
    other_pins: 另一组此刻钉着的 IP。只用来告警, 不改选择(改选择属阈值/策略变更)。"""
    doms = targets_for(name, pinned_ip, parse_hist(router_cmd("uci show dhcp | grep address || true") or ""))
    for hs in load_hotspares(name):
        ip = hs.get("ip")
        if not ip or ip == pinned_ip:
            continue
        sp = steady(ip, host=DOMAIN[name], path=TESTPATH[name])
        print(f"{ts()} [{name}] 热备 {ip} (记录 {hs.get('steady')}MiB/s) 实测 {sp:.1f}MiB/s", flush=True)
        if sp >= floor_mibs:
            if ip in other_pins:
                # 2026-11-07 09:37 的 results.json: Cn2 的 hotspares[1] = 125.88.64.72 = Cn1 钉着的 IP。
                # 名单按运营商分散, 没按"另一组钉着的 IP"去重 -> 单节点故障同时打掉两队。实测挡住了
                # "切到死的", 但那份独立性是假的, 得让人看见。
                print(f"{ts()} [{name}] [warn] 热备 {ip} 同时是另一组的钉住 IP: "
                      f"单节点故障会同时打掉两组(名单缺跨组去重)", flush=True)
            if router_apply(doms, ip):
                print(f"{ts()} [{name}] 止血成功 => 切 {ip} ({len(doms)} 个域名: {' '.join(doms)})", flush=True)
                record_pin(name, ip, sp, old_ip=pinned_ip)   # 止血即记账: 此刻仍在 .run.lock 内
                return ip
            else:
                print(f"{ts()} [{name}] 热备 {ip} 达标但 uci 写入/重启失败, 试下一个", flush=True)
    return None


def classify(sp, rec, floor=None, untrusted=False):
    """返回 (kind, 打印用label)。kind: dead / slow / degraded / ok / unmeasured

    v2.3.31 两个参数:
    - `floor`: 组内"记录值够不够做相对判定"的门槛。默认 25.0(= 老行为), main 传
      `DEGRADE_MIN_RECORD.get(name, DEGRADE_MIN_RECORD_DEFAULT)`。给 Cn2 传 10.0 的原因写在
      常量注释里: 那组的记录区间是 21~24, 用 25 当门槛等于**永久关掉相对劣化档**。
    - `untrusted`: 本轮 canary 被拒(401/403/429)。此时 steady=0 **不能**读成"节点死了" ——
      限流/风控同样让吞吐归零。返回 `unmeasured`, 它既不进 bad 也不触发止血。
      `unmeasured != ok` 但也不是 dead: 这两件事在日志里必须长得不一样, 否则下一个人会去查
      一台其实好好的节点。
    """
    if sp == 0:
        if untrusted:
            return "unmeasured", "unmeasured(canary 被拒, 0 不可信)"
        return "dead", "dead"
    if sp < THRESHOLD_MIBS:
        return "slow", f"slow({sp:.1f}<{THRESHOLD_MIBS})"
    lim = DEGRADE_MIN_RECORD_DEFAULT if floor is None else floor
    if rec >= lim and sp < rec * DEGRADE_RATIO:
        return "degraded", f"degraded({sp:.1f} < 记录 {rec:.1f}*{DEGRADE_RATIO})"
    return "ok", "ok"


def retest_cooldown_left(name=None):
    """距上次定向重测还差多少秒。0 = 可以重测(无标记/标记过期/标记不可读都当可以)。

    标记文件由 xbox-speedtest.py 开头写(epoch 秒), 本程序只读 —— 单写者, 不会两边各写一份口径。
    未来时间戳(时钟回拨/手工写错)按"没见过"处理, 否则一次误写能把重测冻到任意久。

    v2.3.34 (审查方 P2-D): 标记**带组维度**了。原来只有一个 epoch, 于是
    `--pool Cn1` 的一次定向重测会把 Cn2 的冷却一起重置 —— 08:30 修了 Cn1, 09:00 Cn2 整组死,
    09:30 的看门狗只止血不重测 Cn2, 最长压 3 小时; 反向同样成立, 手工跑一次调试也会误伤另一组。
    现在文件是 {"Cn1": epoch, "Cn2": epoch}; **兼容旧格式**(纯数字): 那种情况下无法归因到组,
    对所有组生效 —— 宁可多压一轮, 也不要在格式升级的瞬间放过一次本该冷却的重测。
    `name=None` 取所有组里最新的那个(保守), 保留给"只想问一句还有没有冷却"的调用方。
    """
    try:
        with open(os.path.join(BASE, RETEST_MARK_NAME), encoding="utf-8") as fh:
            raw = (fh.read() or "").strip()
    except Exception:
        return 0
    if not raw:
        return 0
    last = None
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            if name is not None:
                last = obj.get(name)
            else:
                vals = [v for v in obj.values() if isinstance(v, (int, float))]
                last = max(vals) if vals else None
        elif isinstance(obj, (int, float)):      # 旧格式: 单格 epoch, 对全部组生效
            last = obj
    except Exception:
        try:                                      # 最旧格式: 一个裸十进制数
            last = int(raw)
        except Exception:
            return 0
    if last is None:
        return 0
    dt = time.time() - float(last)
    if dt < 0:
        return 0
    return max(0, int(RETEST_COOLDOWN - dt))


def run_lock_taken():
    """拿"测速主程序正在跑"的锁 —— **与 xbox-speedtest.py 的 .run.lock 是同一把**。
    两家都会写同一个路由器, 各拿一把锁等于没锁。2026-11-07 09:28 真机现场: 手动全量跑到
    09:30:56, 09:30 的看门狗在这中间把 Cn2 从 58.251.60.41 切到 14.29.101.160
    (58.251.60.41 那一刻 31.0MiB/s => degraded), 全量却在 09:30:56 按**开始时的** uci 记账
    "kept 58.251.60.41 84.9" —— speed.log 说没换, 路由器实际换了。这不是"差一点"的竞态:
    两个进程的 uci 快照一个来自 09:28、一个来自 09:30, 谁后写谁赢, 而 results.json 只描述
    它自己那份快照。共用锁之后, 看门狗在测速期间直接跳过（下一小时再来）。
    返回: fd(用完 close) / None(平台无 fcntl, 非 Linux 调试) / False(被占着) / LOCK_ERR(锁文件建不了)
    """
    try:
        import fcntl
    except ImportError:
        return None
    try:
        fd = open(os.path.join(BASE, ".run.lock"), "w")
    except Exception:
        # 建不了锁文件: 这既不是"测速在跑", 也不该静默 —— 交由 main 打专门的日志
        return LOCK_ERR
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        fd.close()
        return False
    return fd


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
    # 版本化 URL 体检放在最前面: 它解释的是"为什么下面可能全是 0"。顺序反了, 人会先看到一堆
    # 坏消息再去猜原因; 而且它不依赖路由器, 所以 ssh 挂掉时这一行反而更有用。
    # v2.3.31: 返回值不再扔掉 —— 见 canary_gate。原来这两行的 bool 谁都不接, 于是 404 那天
    # 一行告警之后照旧 classify(0.0)=dead -> 试热备(每个 256MiB 全 0) -> 定向重测,
    # 每小时约 2GiB 打在一个已经不存在的 URL 上, 还把钉住的 IP 换掉。
    gate, gate_state = canary_gate()
    if gate == "gone":
        print(f"{ts()} [canary] 撤版闸门: {gate_state} —— 本轮**不判死、不止血、不重测**。"
              f"0.0 不是节点的问题: 现在拿它止血等于把钉住的 IP 换成一个没验证过的节点,"
              f"还要白烧约 2GiB。先去换 build(测速与体检用的是同一个 URL)", flush=True)
        return
    if gate == "denied":
        print(f"{ts()} [canary] 出口被拒({gate_state}): 本轮 steady=0 记 `unmeasured`, 不判死、不止血。"
              f"限流/风控和「节点坏了」在吞吐上长得一模一样, 这一档只能等下一小时", flush=True)
    elif gate == "unknown":
        print(f"{ts()} [canary] 服务端没给内容({gate_state}): 5xx 偶发是常态, 本轮不下结论、照常体检",
              flush=True)
    untrusted = (gate == "denied")
    # P3-7: 两个程序测同一个节点用的跨度不同(这里 256MiB, 主程序 RANGE_SPAN=160MiB)。
    # 跨度**不动**(改口径已两次引发事故, v2.3.14 用真机证明 256MiB 能过), 但要让人看见 ——
    # 否则 `hotspares.steady` 与本轮的 `speed` 被当成同一把尺子。
    # 下面那个 160MiB 是**抄来的字面量**, 不是从 RANGE_SPAN 读的: 本程序不该为了打一行日志去
    # import 主程序(它崩了看门狗就一起崩, 而看门狗存在的意义正是主程序出问题时还在跑)。
    # 抄一份就有漂移风险, 所以审计 C79 钉着「这行里的数 == 主程序的 RANGE_SPAN>>20」——
    # 改跨度时这行忘了改会红, 而不是让人从一行假数里推错尺子(v2.3.32, 审查方 P2-C)。
    print(f"{ts()} [span] 本程序稳态跨度 {STEADY_MAX_BYTES >> 20}MiB × {STEADY:.0f}s 窗口; "
          f"xbox-speedtest 的 RANGE_SPAN 是 160MiB —— 同一节点两边的数只在本窗口内可比", flush=True)
    pins = get_pins()
    if pins is None:
        print(f"{ts()} [skip] 路由器 ssh 不可达(读 uci 失败): 本轮不体检也不止血, 去查 ssh/密钥/路由", flush=True)
        return
    if not any(pins.values()):
        print(f"{ts()} [skip] 路由器可达但没有任何 address 记录(空配置/首次部署): "
              f"这不是网络问题, 去查 dhcp.@dnsmasq[0].address 是否被清空, 或等 04:00 全量重钉", flush=True)
        return
    bad, spd = [], {}
    for name in DOMAIN:
        ip = pins.get(name)
        if not ip:
            bad.append(name)
            print(f"{ts()} [{name}] 无钉住 IP => 异常", flush=True)
            continue
        sp = steady(ip, host=DOMAIN[name], path=TESTPATH[name])
        rec = load_record(name, ip)   # 基准跟着实际钉住 IP 走(止血后基准必须换成热备自己的记录)
        if rec <= 0 and sp >= THRESHOLD_MIBS:
            # "没触发 degraded" 与 "没有基准所以没判" 在日志里必须长得不一样, 否则基准丢了看不见
            print(f"{ts()} [{name}] 无劣化基准(rec=0) => 本轮只判 dead/slow, degraded 不参与", flush=True)
        kind, label = classify(sp, rec, floor=DEGRADE_MIN_RECORD.get(name, DEGRADE_MIN_RECORD_DEFAULT),
                               untrusted=untrusted)
        if kind == "unmeasured":
            # 0 分不是"死", 也不该复测(复测还是 0, 只是多花 8s): 直接记账跳过, 不进 bad。
            spd[name] = sp
            print(f"{ts()} [{name}] {ip} steady={sp:.1f}MiB/s => {label}", flush=True)
            continue
        if kind != "ok":
            # 单次 8s 采样在晚高峰会抖(实测 108 的节点偶发掉到 40): 复测一次取高值,
            # 两次都低才动手 —— 否则会把好节点换成更慢的热备, 典型的"越止越慢"
            sp2 = steady(ip, host=DOMAIN[name], path=TESTPATH[name])
            if sp2 > sp:
                print(f"{ts()} [{name}] 复测 {sp2:.1f}MiB/s (首测 {sp:.1f}MiB/s), 判据取高值", flush=True)
                sp = sp2
                kind, label = classify(sp, rec, floor=DEGRADE_MIN_RECORD.get(name, DEGRADE_MIN_RECORD_DEFAULT))
        spd[name] = sp
        print(f"{ts()} [{name}] {ip} steady={sp:.1f}MiB/s => {label}", flush=True)
        if kind != "ok":
            bad.append(name)
    if bad:
        # 第一步: 热备止血(分钟级), 有钉住 IP 的坏组才试
        # 先拿**测速主程序那把锁**: 测速期间改路由器会让两份 uci 快照打架(见 run_lock_taken 注释)
        lk = run_lock_taken()
        if lk == LOCK_ERR:
            # 与"测速在跑"分开说: 这一条意味着 BASE 不可写, 止血会一直跳过 —— 要去查权限/磁盘, 不是查并发
            print(f"{ts()} [skip] 无法创建 .run.lock(权限/磁盘/目录只读): 本轮不动路由器。"
                  f"这不是\"测速在跑\", 别去查并发进程", flush=True)
            return
        if lk is False:
            print(f"{ts()} [skip] 测速主程序正在跑(共用 .run.lock): 本轮不止血也不重测, 下一小时再来",
                  flush=True)
            return
        try:
            for name in bad:
                if pins.get(name):
                    # 达标线: 死/极慢用固定门槛; 只是劣化则必须比当前实测再快 10%, 否则越止越慢
                    floor = HOTSPARE_MIN_MIBS
                    if spd.get(name, 0) >= THRESHOLD_MIBS:
                        floor = max(HOTSPARE_MIN_MIBS, spd[name] * 1.1)
                    # v2.3.34 (审查方 P1-C): 返回值必须接住。原来这里裸调, `pins` 还停在
                    # get_pins() 那一刻的值, 于是第二组算 other_pins 时看到的是第一组的**旧** IP。
                    # 后果: 两组同轮都止血到同一个节点, 而 `other_pins` 那条跨组 warn
                    # (「单节点故障会同时打掉两组」)一次都不会响 —— 它比的是"对方上一轮钉着谁",
                    # 不是"对方这一轮切到了哪"。切完就更新, warn 才有对象。
                    _r = quick_fix(name, pins[name], floor,
                                   other_pins={v for k, v in pins.items() if k != name and v})
                    if _r:
                        pins[name] = _r
        finally:
            if lk:
                lk.close()      # 必须先放锁再拉子进程: 子进程自己要拿同一把锁
        # 第二步: 定向重测坏组(speedtest 会读到止血后的新钉住 IP, 迟滞保护不会立刻换走)
        # 冷却只挡第二步: 止血是分钟级、几 MB 的事, 任何情况下都照做; 被挡的是每次 3.71GiB 的重测。
        # v2.3.34 (审查方 P2-D): 冷却**按组**判。原来整轮共用一个 epoch, 一组被重测就把另一组
        # 一起压住 3 小时 —— 而"哪一组该重测"与"上一次重测的是哪一组"本是两件事。
        groups = list(dict.fromkeys(bad))
        _fire = []
        for g in groups:
            _left = retest_cooldown_left(g)
            if _left > 0:
                print(f"{ts()} [cooldown] {g} 上次重测在 {RETEST_COOLDOWN - _left}s 前, "
                      f"冷却剩 {_left}s => 该组只止血, 本轮不重测", flush=True)
            else:
                _fire.append(g)
        if _fire:
            args = []
            for g in _fire:
                args += ["--pool", g]
            print(f"{ts()} [trigger] 需重测的组 {_fire}"
                  + (f" (冷却中未触发: {[g for g in groups if g not in _fire]})"
                     if len(_fire) < len(groups) else "")
                  + f" => 触发定向重测 {' '.join(args)}", flush=True)
            # 只看 [trigger] 分不出"重测跑完了"与"子进程被 OOM/异常杀掉": 旧实现既不设 timeout 也不看
            # 退出码(2026-11-07 外部审计 B3-②)。一次实测最长 3405s, 取 5400s 作上限: 超了就是卡死,
            # 让本进程收手并留下证据, 而不是挂着 .watchdog.lock 把下一小时的体检也一起堵死。
            try:
                r = subprocess.run([sys.executable or "python3", os.path.join(BASE, "xbox-speedtest.py")] + args,
                                   cwd=BASE, timeout=RETEST_TIMEOUT)
                print(f"{ts()} [retest] 子进程退出码 {r.returncode}"
                      + ("" if r.returncode == 0 else " (非 0: 本轮重测没跑成, 查 speed.log 是否多了一行)"),
                      flush=True)
            except Exception as e:
                print(f"{ts()} [retest] 子进程未正常结束({e.__class__.__name__}: {e}) => 本轮无结论", flush=True)
    else:
        print(f"{ts()} [all-ok] 各组健康, 无动作", flush=True)


if __name__ == "__main__":
    main()
