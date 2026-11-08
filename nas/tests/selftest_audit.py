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
  M78 注解竖线 None (pool_refresh.py 写 def f(x: int 竖线 None): 3.8 定义期即 TypeError)  -> A22
  M79 注释被判成代码 (只在 pool_refresh.py 顶部加一行注释写 int 竖线 None: A22 必须仍绿)  -> GREEN
  M80 README 数字漂移 (RTT_TOP 48->72 而 README 仍用 48 论证: 137 项曾全绿)  -> A23
  M81 样本丢快测表 (build_sample 的 quick 置空: 落盘的分布缺了最要紧的那半边)  -> A24
  M82 样本被覆盖 (append 改 "w": 当天分布只剩最后一轮, 前面几轮静默消失)      -> A24
  M83 样本漏进结论 ("_sample" 挂回返回值: results.json 被分布灌胖)            -> A24
  M84 canary 写了不调用 (main 不问闸门: 档位再准也进不了决策)                       -> A25
  M85 canary 只认 200 (正常 206 每小时误报成"URL 下架": 狼来了)                -> A25
  M86 canary 绑 IP (节点坏了伪装成 URL 下架: 假警报比不报更有害)               -> A25
  M87 canary 走 HTTPS (证书不是给这个域名签的: 真 URL 每小时 SSLCertVerificationError)  -> A25
  M88 样本路径写回模块常量 (join(BASE) 在 import 时算死: 审计跑 main 重定向不动, 写脏真文件)  -> A24
  M89 样本路径绕开 BASE (用 __file__ 现拼: 审计的临时目录拦不住, 夹具样本照样落进交付目录)  -> A26
  M90 无冠军那轮不交样本 (sink 判断写反: 坏日子的分布静默消失, 回放只见好天气)  -> A24
  M91 canary 把所有 4xx/5xx 当撤版 (403 被限流 / 5xx 偶发都说成文件没了, 照着换 build)  -> C71
  M92 截断尾部不填 (只加 tail 参数不 extend: l0_tail 恒空, 与"池子刚好 48 个"分不开)  -> A24
  M93 池子计数不填 (stats 不 update: l0_pool/probed/alive 全是 None, 空 tail 分不清没漏还是没测完)  -> A24
  M94 canary 闸门恒 ok (档位收集了但不比: 撤版那轮照旧判 dead -> 试热备, 每小时 2GiB 空转)  -> C73
  M95 classify 无视 floor (分组门槛压成默认 25: Cn2 从 24 掉到 9 判 ok, 相对劣化档恒关)  -> C74
  M96 load_ips 去掉读侧校验 (池里的内网地址照收: 测出高分 -> 钉进 dnsmasq -> 整组断网)  -> C75
  M97 粗筛同分按 IP 降序 (回到旧实现: 8MiB 全撞地板, 冠军由字典序决定)  -> C76
  M98 交付集合漏一行 (PAIRS 去掉 tests/replay.py: push 永远不带它, check 报不出原因)  -> D8
  M99 粗筛不记并发 (wave_out 不填: 同质节点分数差 4 倍, 样本里却没有『被几路平分』)  -> C77
  M100 零分不回报 (zero_out 不填: 『预算没测』与『测了是 0』在样本里同形)  -> C78
  M101 跨度字面量漂移 (watchdog 的 [span] 写 140MiB 而主程序 160: 观测变成假数)  -> C79
  M102 空 ms_of 回 inf (第二键失效、第三键接管: 握手最快的排到最后)  -> C80
  M103 replay 默认 None (不带参数就不回放: 翻转恒 0, docstring 却说默认 1.15)  -> C81
  M104 WANTED 少一键 (回放器不再点名 screen_zero: 三态对称又断一半)  -> C78
  M105 need 记成 floor (screen_need 硬写 80.0: 两个量又混成一个, 『撞不到』的解释没了)  -> C82
  M106 常量把并发摊进节点上限 (线变 20, 实测 P50 33 -> 41/48 超线而 capped 空)  -> C83
  M107 消费点再除一次并发 (回归 v2.3.33 真 bug: _floor_line=80/wave_max, 4 路变 20.0)  -> C84
  M108 合并抹掉 POOLS 外的组 (删原地保留那行: 临时注释掉某组后的存留被顺手删)  -> C86
  M109 旧表读不出当正常 (非 list 也 readable=True: 拿 [] 当没有旧账, 另一组被静默抹掉)  -> C86
  M110 止血返回值不接 (pins 停在 get_pins 那刻: 两组同轮切同一 IP 时跨组 warn 永不响)  -> C87
  M111 冷却不分组 (retest_cooldown_left 忽略 name: 一组重测压住另一组 3 小时)  -> C88
  M112 分段耗时只留 t_l0 (删 t_screen: 3405s 一轮事后算不出哪一段在变慢)  -> C89
  M113 canary 又把 bool 还回来 (return ok: False 同时代表 gone/denied/unknown/failed)  -> C90
  M114 回退成"读不出就不写" (坏表永不被重写: 劣化档永久关闭 + 止血恒无备胎, 且自我维持)  -> C86b
  M115 三态压回同一个键 (skip 与 zero 同权: 截断先扔掉"未知"节点)                        -> C91
  M116 极差吃 0 值 (把"一轮没测出来"说成"抖动", 处置方向正好相反)                        -> C92
  M117 screen_wave 只存一个 max (回退成"不是逐 IP", 4 路分摊就真的测不了了)            -> C93
  M118 status 不区分 created / created_after_writefail (写失败与首次部署同形)           -> C86b
  M119 隔离文件名不再加序号 (同一秒两次隔离, 前一份坏证据被覆盖)                        -> C94
  M120 replay 2c 不分组 (Cn1/Cn2 混进一张表: 两个分布的中位数没有意义)                -> C95
  M121 created_after_writefail 不打印 (status 分细了但无人值守日志里仍然同形)           -> C86b
  M122 2c 不按配置分桶 (丢 screen_workers: W=1 的 76.0 与 W=4 的 22.0 同桶)             -> C96
  M123 字段完整率不看首现行 (老格式缺席与新版本丢字段压成同一个 N)                     -> C97
  M124 WANTED 删 note (唯一承载判定理由的字段, 整列消失也全绿)                        -> C98
  M125 配不上的 IP 静默丢弃 (只有 screen 没有 wave 的条目不计数、不报)                -> C93
  M126 配置指纹退回 screen_need (派生量当配置: 劣化夜被读成"另一种并发", 凭空出 Δ)      -> C99
  M127 坏夜行算成字段缺损 (decision='no winner' 的行 8 个决策字段长期挂"缺 1 行")       -> C100
  M128 need 与 80×max(wave) 不符时沉默 (推导口径变了还继续画表)                        -> C101
  M129 跨配置 Δ 不看 n (一侧只有一次测量也报百分号)                                    -> C96
  M130 第六节真值不剔本轮 (拿答案对答案: Top1 命中 1/3 -> 2/3, 召回率被系统性抬高)      -> C102
  M131 _topk 改升序 (同一把尺子翻了: 名次与重合率变成排序实现的函数)                   -> C103
  M132 不足 3 轮也出召回结论 (两轮的中位就是一次测量, 却照样给 N 的建议)               -> C104
  M133 wave 自证恒 OK (并发没生效的格照样进表, 整档数与别档不可比)                    -> C105
  M134 审计不删自己造的临时目录 (NAS /tmp 累积 23.8 万条, 自检卡在 ENOSPC)             -> D9
  M135 第七节全部快测先跑 (逐 IP 反向没了: 秩相关含时间漂移, 是时钟的函数)            -> C106
  M136 第七节秩相关把 0 值也算进来 (没测出被当成最慢参与排序)                          -> C107
  M137 from-json 配 stage steady 静默跳过 (不做事、不报错、还退出 0)                   -> C108

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
FILES = ["xbox-speedtest.py", "watchdog.py", "pool_refresh.py", "nas-README.md", "deliver.sh",
         # v2.3.37: replay.py 也进清单 —— C95 用**合成样本跑真 replay** 断言 2c 按组拆表,
         # 它读的是 d/tests/replay.py(不存在才退回审计器同目录)。不放进来的话, 那条断言
         # 永远打不到"混组"这个变异, 等于自带免疫。
         os.path.join("tests", "replay.py"),
         # v2.3.39 第四十轮: 探针也进清单 —— C102~C105 读的是 `d/tests/exp_screen_workers.py`,
         # 打它的变异(M130~M133)必须改到**变异目录里那一份**。这里不能再靠下面那个
         # `fn.startswith("tests/")` 支路: `os.path.join` 在 Windows 上给的是 `tests\xxx`,
         # 那个判断恒为假, 于是探针根本没被复制过来 —— 变异会以 FileNotFoundError 收场,
         # 看起来像"注入点不存在", 实际是自检自己没把被审对象带进沙箱。
         os.path.join("tests", "exp_screen_workers.py")]
# deliver.sh 必须在这份清单里: 变异目录只有放进来的文件, 而 `D8` / `D-crlf:deliver.sh` 在
# audit.py 里是 `isfile` 守着的 —— 不在清单里就**静默跳过**, 于是打它自己那两条变异(现在是 M98)
# 会以「注入点不存在」或 FileNotFoundError 收场。这不是"多复制一个文件", 是让交付脚本本身可被反证。
#
# 但 deliver.sh **只存在于本地镜像侧**: 它是推送工具, 不在 deliver.sh 自己的 PAIRS 表里, NAS 上
# 没有这个文件。所以复制时按"本侧实际有哪些"收口, 拿不到的记进 MISSING, 打它自己的变异按 SKIP 记
# (不掺进分母), 而不是让整轮自检在第一行就 FileNotFoundError。**在哪一侧缺、缺了哪条变异, 必须
# 打出来**: 静默缩表比报错更难发现 —— 上一版就是这么把 `FILES` 漏成四个还一声不响的。
MISSING = [f for f in FILES if not os.path.isfile(os.path.join(SRC, f))]
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
     '    tag = "" if not _few else f"仅 {len(good)}/{len(rs)} 轮出数(其余轮 0=没测出), "',
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
     "            _left = retest_cooldown_left(g)", "            _left = retest_cooldown_left(g) * 0"),
    ("M38", "未来时间戳不设防 (时钟回拨/误写一次就能把重测冻死)", "watchdog.py", "C48d",
     "    if dt < 0:\n        return 0\n", ""),
    # 多行注入点一律用三引号直写: 用 "\n" 转义时, 生成脚本里的反斜杠被处理坏过一次(整条变异静默失效)
    # v2.3.34: 读标记改成 JSON 后原来的 `with open(...) / last = int(...)` 两行没了 —— 注入串换成
    # "读完标记、还没解析"那一格, 在那里插一句写, 正好是"看门狗也去写标记"的形态。
    ("M39", "双写者 (watchdog 也去写冷却标记, 两边口径迟早不同)", "watchdog.py", "C49",
     '''    if not raw:
        return 0
    last = None
''',
     '''    open(os.path.join(BASE, RETEST_MARK_NAME), "w", encoding="utf-8").write("0")
    if not raw:
        return 0
    last = None
'''),
    # v2.3.34: 标记写的是 JSON `{group: epoch}`。这条的形态仍是"打了标记但等于没打"——
    # 整段写盘换成 pass。它同时会红 C49(写者 0 个), 那是同一个动作的第二个侧面, 不算假红。
    ("M40", "主程序不打标记 (看门狗拿不到冷却依据, 退化成无退避)", "xbox-speedtest.py", "C49b",
     '''        with open(os.path.join(BASE, RETEST_MARK_NAME), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(cur, ensure_ascii=False, sort_keys=True) + "\\n")
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
     '    mark_retest([p0[0] for p0 in pools])',
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
     "`xbox-speedtest/tests/audit.py` 的 **A18**", "`xbox-speedtest/tests/audit.py` 的 **C199**"),
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
    # M78/M79 是一对: 同一段 A22 代码的两个方向。M78 证明它抓得到真注解(唯一在 3.8 上会炸的形态),
    # M79 证明它**不**抓注释 —— 2026-11-07 之前 A22 是全文正则 `竖线 None`, 注释里写一句就假红,
    # 而假红让 M78 根本写不进来(A22 扫的 selftest 是真实路径, 变异串自己就被判红) -> 那半边永久无覆盖。
    ("M78", "注解竖线 None 漏检 (pool_refresh.py 写 def f(x: int 竖线 None): 3.8 定义期即 TypeError)",
     "pool_refresh.py", "A22",
     'if __name__ == "__main__":\n    main()',
     'if __name__ == "__main__":\n    main()\n\ndef _probe(x: int | None):\n    return x'),
    ("M79", "注释被判成代码 (只在 pool_refresh.py 顶部加一行注释写 int 竖线 None: A22 必须仍绿)",
     "pool_refresh.py", "GREEN",
     '# -*- coding: utf-8 -*-',
     '# -*- coding: utf-8 -*-\n# 历史上这里返回 int | None'),
    # M80: README 11 处把常量值当论据、零断言。改代码不改文档 = 句子照样通顺、结论已经失效。
    ("M80", "README 数字漂移 (RTT_TOP 48->72 而 README 仍用 48 论证「48 刚好不漏」: 曾是 137 项全绿)",
     "xbox-speedtest.py", "A23",
     "RTT_TOP = 48", "RTT_TOP = 72"),
    # M81-M83 打的是 samples.jsonl 这条新链。三条各自对应一种"看起来落了盘、实际数据废了"的形态:
    # 丢字段(分布缺最要紧的半边) / 覆盖写(只剩最后一轮) / 漏进结论文件(把 watchdog 读的 results.json 灌胖)。
    ("M81", "样本丢快测表 (build_sample 的 quick 置空: 落盘的分布缺了最要紧的那半边)",
     "xbox-speedtest.py", "A24",
     '"quick": {ip: round(sp, 1) for ip, sp in quick.items()},',
     '"quick": {},'),
    ("M82", "样本被覆盖 (append 改 \"w\": 当天分布只剩最后一轮, 前面几轮静默消失)",
     "xbox-speedtest.py", "A24",
     'with open(_path, "a", encoding="utf-8") as f:',
     'with open(_path, "w", encoding="utf-8") as f:'),
    # M83 换过锚点: 样本改成走 sample_sink(返回值里不再有 _sample), 原来那句 pop 的变异就成了空转 ——
    # 自测当场报"漏网 M83", 而它漏的不是断言, 是变异自己失去了对象。等价缺陷现在是"把样本又挂回
    # 返回值": 那样它会跟着 r 进 results.json(main 只 pop 了 ranked/quick/rows/tag_of/_t)。
    ("M83", "样本漏进结论 (把 \"_sample\" 挂回返回值: results.json 被分布灌胖, watchdog 读的文件变样)",
     "xbox-speedtest.py", "A24",
     '            "tag_of": tag_of, "ranked": results}',
     '            "tag_of": tag_of, "ranked": results, "_sample": _smp}'),
    # M84-M86 打 canary 的三条边: 不调用(等于没加) / 只认 200(正常 206 每小时误报成下架) /
    # 绑 IP(单台节点坏了伪装成 URL 下架)。后两条都是"能报警但报错", 比不报更能训练人忽略日志。
    # v2.3.31: 注入点跟着实现走 —— main 里那两行从 `for _cn in CANARY: canary(_cn)` 换成了
    # `gate, gate_state = canary_gate()`。旧注入串在本轮"注入点不存在"漏网了一次(自检自己失效),
    # 所以这条现在钉的是**闸门被调用**: 结果接不接得住是 C73 的事, A25 只管"main 有没有真去问"。
    ("M84", "canary 写了不调用 (main 里闸门没被问过: 档位再准也进不了决策)",
     "watchdog.py", "A25",
     "    gate, gate_state = canary_gate()", '    gate, gate_state = "ok", {}  # 变异: 不问闸门'),
    ("M85", "canary 只认 200 (正常 206 每小时误报成\"URL 下架\": 狼来了)",
     "watchdog.py", "A25",
     "ok = st in (200, 206)", "ok = st in (200,)"),
    ("M86", "canary 绑 IP (单台节点坏了伪装成 URL 下架: 假警报比不报更有害)",
     "watchdog.py", "A25",
     "conn = http.client.HTTPConnection(DOMAIN[name], timeout=DL_TIMEOUT)",
     "conn = BindIPConn(DOMAIN[name], \"1.1.1.1\", DL_TIMEOUT)"),
    # M87 是真机踩出来的: 一开始写的是 HTTPSConnection, NAS 上一跑, 真 URL 直接
    # SSLCertVerificationError(Hostname mismatch) —— 证书不是给 dlassets.xboxlive.cn 签的。
    # 下载路径是明文 HTTP + Host 头, 从没做过 TLS 校验; canary 用 HTTPS 等于每小时报一次假警。
    ("M87", "canary 走 HTTPS (真 URL 每小时 SSLCertVerificationError: 给体检加了体检对象不遵守的门)",
     "watchdog.py", "A25",
     "conn = http.client.HTTPConnection(DOMAIN[name], timeout=DL_TIMEOUT)",
     "conn = http.client.HTTPSConnection(DOMAIN[name], timeout=DL_TIMEOUT)"),
    # M88/M89 打的是 v2.3.28 真踩出来的那条: 样本路径**必须**在调用时用 BASE 拼。
    # M88 是原缺陷形态(模块常量, import 时算死) —— 审计里 38 个跑 main 的用例改 BASE 也重定向不动,
    # 夹具样本(9.9.9.9/1.0ms)全进真文件; 靠人眼在 39 行里挑不出来, 因为假行字段和真行长得一样。
    ("M88", "样本路径写回模块常量 (import 时算死: 审计改 BASE 重定向不动, 38 行夹具样本混进真文件)",
     "xbox-speedtest.py", "A24",
     'BASE = os.path.dirname(os.path.abspath(__file__))',
     'BASE = os.path.dirname(os.path.abspath(__file__))\nSAMPLES = os.path.join(BASE, "samples.jsonl")'),
    # M89 是**绕过** BASE 的形态(__file__ 现拼): A24 的文本形态照样满足, 只有事后对账(A26)能抓 ——
    # 这条存在的意义就是证明 A26 不是装饰: 断言"字节没变", 不依赖"用例记得重定向"这种自觉。
    ("M89", "样本路径绕开 BASE (用 __file__ 现拼: 临时目录拦不住, 夹具样本落进交付目录)",
     "xbox-speedtest.py", "A26",
     '_path = os.path.join(BASE, "samples.jsonl")',
     '_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples.jsonl")'),
    # M90: sink 判断写反 = 只在"没有 sink"时交样本。好日子里看着照常, 缺的恰好是坏日子 ——
    # 而"坏日子没留样本"正是回放最大的失真来源(会得出"这网一直很稳")。
    ("M90", "无冠军那轮不交样本 (sink 判断写反: 坏日子的分布静默消失)",
     "xbox-speedtest.py", "A24",
     "    if sample_sink is not None:" + chr(10) + "        sample_sink.append(_smp)",
     "    if sample_sink is None:" + chr(10) + "        sample_sink = [_smp]"),
    # M91: 把 401/403/429 与 5xx 一起当 404 报 —— 运维会照着"换 build"动手, 而真因可能是限流。
    ("M91", "canary 把所有 4xx/5xx 当撤版 (403 被限流 / 5xx 偶发都说成文件没了)",
     "watchdog.py", "C71",
     "        if st in (404, 410):",
     "        if st >= 400:"),
    # M92/M93 是"只加断言不加反证"的解药: 这两行删掉之后 A24 的 (g) 仍然会红,
    # 但如果没有它们, 自检只能证明"断言写着", 证明不了"断言还活着"。
    ("M92", "截断尾部不填 (只加 tail 参数不 extend: l0_tail 恒空, 与\"池子刚好 48 个\"分不开)",
     "xbox-speedtest.py", "A24",
     "        tail.extend([round(_m, 1), _ip] for _m, _ip, _t in out[top:top + 8])",
     "        pass  # 变异: 只加签名不填 tail"),
    ("M93", "池子计数不填 (stats 不 update: 空 tail 分不清\"没漏\"还是\"没测完\")",
     "xbox-speedtest.py", "A24",
     '        stats.update({"pool": len(uniq), "probed": done, "alive": len(best)})',
     "        pass  # 变异: 不交代池子大小"),
    # ---- M94..M97 对应 v2.3.31 新增的四条断言(C73/C74/C75/C76) ----
    # 这四条断言的共同点: 被测的东西**在旧口径下全都"看起来正常"**。
    #   撤版那天: 闸门恒 ok -> 照旧判 dead -> 试热备 -> 每小时 2GiB 打在一个 404 上;
    #   Cn2 门槛 25: classify(9.0, 23.9) 返回 ok, 从 24 掉到 9 叫"稳定";
    #   读侧不校验: 内网地址被测出高分 -> 被钉进 dnsmasq -> 整组断网;
    #   同分按 IP 排: 80MiB/s 以上全撞地板, 谁是冠军由字典序决定。
    # 没有这四行, C73~C76 只是"写着", 证明不了"还活着"。
    ("M94", "canary 闸门恒 ok (档位收集了但不进判断: 撤版那轮照旧判 dead -> 试热备 -> 每小时 2GiB 空转)",
     "watchdog.py", "C73",
     '    for want in ("gone", "denied", "unknown"):',
     '    for want in ("__never__",):  # 变异: 三档一个都不比, 恒落到 return "ok"'),
    ("M95", "classify 无视 floor (分组门槛被压成默认 25: Cn2 从 24 掉到 9 判 ok, 相对劣化档恒关)",
     "watchdog.py", "C74",
     "    lim = DEGRADE_MIN_RECORD_DEFAULT if floor is None else floor",
     "    lim = DEGRADE_MIN_RECORD_DEFAULT  # 变异: 不看传进来的分组门槛"),
    ("M96", "load_ips 去掉读侧校验 (池文件里的内网地址照收: 被测出高分 -> 钉进 dnsmasq -> 整组断网)",
     "xbox-speedtest.py", "C75",
     "        why = ip_reject_reason(ip)",
     "        why = None  # 变异: 只校验写侧, 读侧全收"),
    # M97 必须还原成**旧的真身**: 旧实现是 `sorted(((-sp, ip) ...), reverse=True)`,
    # 也就是同分时按 IP **降序**。写成升序会与「按 ms 排」偶然同序, C76 照样绿 = 假漏网。
    ("M97", "粗筛同分按 IP 降序 (回到旧实现: 8MiB 全撞 SPEED_MIN_EL 地板, 冠军由字典序决定)",
     "xbox-speedtest.py", "C76",
     '''    ranked = sorted(((-sp, _k2(ip), ip) for ip, sp in speeds.items() if sp > 0))
    return [(-neg, ip) for neg, _ms, ip in ranked]
''',
     '''    ranked = sorted(((-sp, ip) for ip, sp in speeds.items() if sp > 0), reverse=True)
    return [(-neg, ip) for neg, ip in ranked]
'''),
    # M98: 交付集合漏一项。D8 原本的 `_want` 只数三个手抄的 tests 名, 于是 replay.py
    # 「README 清单里有、PAIRS 里没有」这件事两边都不报 —— push 永远不带它, 而 check 只会红
    # 在一条指不出原因的单侧项上。这条变异钉住的是「交付集合只有一个来源」。
    ("M98", "交付集合漏一行 (PAIRS 去掉 tests/replay.py: push 永远不带它, check 报不出原因)",
     "deliver.sh", "D8",
     '  "tests/replay.py:$RD/tests/replay.py"\n', ""),

    # ---- M99..M104 对应 v2.3.32 新增的五条断言(C77/C78/C79/C80/C81) ----
    # 这一批的共同点: 缺陷都在**样本/日志这一侧**, 不影响任何一条决策分支 —— 也就是说
    # 只看"跑出来的冠军对不对"永远发现不了。它们各自的代价都是"半年后想归因时才发现没记"。
    # M99 并发不记: 12 个同质节点的分数差 4 倍, 而样本里没有"被几路平分过"这一列 -> 无法归因
    # M100 零分不回报: "预算没测"与"测了是 0"在样本里同形(键不存在) -> 下一轮该补测还是该拉黑, 分不清
    # M101 span 字面量漂移: 主程序改跨度, [span] 照旧念 160 -> 观测本身变成假数
    # M102 空 ms_of: 第二键整段变 inf, 第三键(IP 字符串)接管 -> 握手最快的排到最后, 而 L0 全不通时正好走这条路
    # M103 replay 默认 None: 不带参数就不回放, 翻转恒 0, 而 docstring 说"默认 1.15"
    # M104 WANTED 少一键: 回放器不再点名 screen_zero -> 三态那条对称又断了一半
    ("M99", "粗筛不记并发 (wave_out 不填: 同质节点分数差 4 倍, 而样本里没有『被几路平分』的凭证)",
     "xbox-speedtest.py", "C77",
     '        wave_out.update(dict((ip, capped[ip].get("wave")) for ip in speeds))',
     '        pass  # 变异: 并发档位不回报'),
    ("M100", "零分不回报 (zero_out 不填: 『预算没测』与『测了是 0』在样本里同形, 含义正相反)",
     "xbox-speedtest.py", "C78",
     "    if zero_out is not None:\n        zero_out.extend(zero)",
     "    if zero_out is not None:\n        pass  # 变异: 0 分不回报, 与『没测到』混在一起"),
    ("M101", "跨度字面量漂移 (watchdog 的 [span] 写 140MiB 而主程序仍是 160: 观测变成假数)",
     "watchdog.py", "C79",
     "xbox-speedtest 的 RANGE_SPAN 是 160MiB", "xbox-speedtest 的 RANGE_SPAN 是 140MiB"),
    ("M102", "空 ms_of 回 inf (第二键整段失效、第三键接管: 握手最快的排到最后, 而 L0 全不通时正好走这条路)",
     "xbox-speedtest.py", "C80",
     '        _k2 = lambda ip: pos.get(ip, len(pos))              # noqa: E731',
     '        _k2 = lambda ip: float("inf")                       # 变异: 退回 inf'),
    ("M103", "replay 默认 None (不带参数就不回放: 翻转恒 0, 而 docstring 说『默认 1.15』)",
     "tests/replay.py", "C81",
     "    new_ratio = DEFAULT_RATIO", "    new_ratio = None  # 变异: 不指定就不回放"),
    ("M104", "WANTED 少一键 (回放器不再点名 screen_zero: 粗筛三态那条对称又断了一半)",
     "tests/replay.py", "C78",
     '"screen", "screen_capped", "screen_skip", "screen_zero", "screen_wave",',
     '"screen", "screen_capped", "screen_skip", "screen_wave",'),

    # ---- M105..M107 对应 v2.3.33 新增的三条断言(C82/C83/C84) ----
    # 共同点: 都是「撞顶线是**每路**的」这件事被写成了别的口径。三种写法都会让
    # `screen_capped` 这一列从「有信息的空」退化成「没信息的空/满」, 而所有决策分支照常跑完。
    # M105 样本硬写串行 80: 读样本的人会以为「4 路下没撞顶」= 节点都没到 80, 其实这一档撞不到
    # M106 常量把并发摊进去: 4 路下这条线变 20, 而实测 P50 就是 21.6 -> 一半以上节点"撞顶", capped 恒满
    # M107 test_pool 不看 wave: 生产路径恒记 80.0, 与 M105 一样抹掉档位, 但 C82 仍绿(它只测 build_sample)
    ("M105", "样本把 need 记成 floor (screen_need 硬写 80.0: 两个量又混成一个, 4 路『撞不到』的解释没了)",
     "xbox-speedtest.py", "C82",
     '        "screen_need": (round(screen_need, 1) if screen_need else None),',
     '        "screen_need": 80.0,   # 变异: 把"达到撞顶所需出口"记成撞顶线本身'),
    ("M106", "常量把并发摊进节点上限 (SPEED_CEIL_MIB 再除以 4: 线变 20, 实测 P50 33 -> 41/48 超线而 capped 空)",
     "xbox-speedtest.py", "C83",
     'SPEED_CEIL_MIB = SCREEN_CHUNK / 1048576 / SPEED_MIN_EL   # 80.0',
     'SPEED_CEIL_MIB = SCREEN_CHUNK / 1048576 / (SPEED_MIN_EL * 4)   # 变异: 把每路线摊成节点线'),
    ("M107", "消费点再除一次并发 (回归 v2.3.33 的真 bug: _floor_line = 80/wave_max, 4 路变 20.0)",
     "xbox-speedtest.py", "C84",
     "    _floor_line = SPEED_CEIL_MIB                                   # 80.0, 恒",
     "    _floor_line = SPEED_CEIL_MIB / (_wave_max or 1)   # 变异: 把'更难达到'记成'门槛下降'"),
    ("M108", "合并时抹掉 POOLS 之外的组 (删掉原地保留那一行: 临时注释掉某组后的存留被顺手删)",
     "xbox-speedtest.py", "C86",
     '    merged += [g for g in by_group.values() if g.get("group") not in known]\n',
     ''),
    ("M109", "旧表读不出当正常 (非 list 也返回 readable=True: 拿 [] 当'没有旧账', 另一组被静默抹掉)",
     "xbox-speedtest.py", "C86",
     "    if not isinstance(existing, list):\n        return [], False",
     "    if not isinstance(existing, list):\n        return [], True   # 变异: 损坏表当正常态"),
    ("M110", "止血返回值不接 (pins 停在 get_pins 那一刻: 两组同轮切到同一 IP 时跨组 warn 永远不响)",
     "watchdog.py", "C87",
     "                    if _r:\n                        pins[name] = _r",
     "                    if _r:\n                        pass   # 变异: 丢掉 quick_fix 的返回值"),
    ("M111", "冷却不分组 (retest_cooldown_left 忽略 name: 一组重测压住另一组 3 小时)",
     "watchdog.py", "C88",
     "            if name is not None:\n                last = obj.get(name)",
     "            if False:   # 变异: 不看组, 永远取全表最新\n                last = obj.get(name)"),
    ("M112", "分段耗时只留 t_l0 (删掉 t_screen: 3405s 一轮事后算不出哪一段在变慢)",
     "xbox-speedtest.py", "C89",
     '        "t_screen": (round(t_screen, 1) if t_screen is not None else None),\n',
     ''),
    ("M113", "canary 又把 bool 返回回来 (return ok: False 同时代表 gone/denied/unknown/failed)",
     "watchdog.py", "C90",
     '    elif state is not None:\n        state[name] = "ok"',
     '    elif state is not None:\n        state[name] = "ok"\n    return ok   # 变异: 把有损副本还回来'),

    # M114-M116 打 v2.3.35 的三条新断言。M114 专门回退成 v2.3.34 的那一刻: 诊断不是"少一个功能",
    # 而是"坏文件从此再也不会被重写", 只有**连跑两轮**才看得出来。
    ("M114", "回退成\"读不出就不写\" (坏表永远不被重写: load_record 恒 0.0、止血恒无备胎, 且自我维持)",
     "xbox-speedtest.py", "C86b",
     '        merged, _ = merge_results([], summary, known)      # 重建: 只剩本轮结论(旧账在隔离文件里)',
     '        return existing, "unreadable"    # 变异: v2.3.34 的老行为(这一轮不写回)'),
    ("M115", "三态压回同一个键 (skip 与 zero 同权: 截断时先扔掉\"未知\"节点)",
     "xbox-speedtest.py", "C91",
     """    if ip in skip:
        return (20, 0.0, 0)""",
     """    if ip in skip:
        return (30, 0.0, 0)   # 变异: 与"测了 0"同权"""),
    ("M116", "极差吃 0 值 (把\"一轮没测出来\"说成\"抖动\", 而两者处置方向相反)",
     "xbox-speedtest.py", "C92",
     '    lo, hi = (min(good), max(good)) if good else (0.0, 0.0)',
     '    lo, hi = min(rs), max(rs)   # 变异: 极差含 0, 与中位数口径不一'),

    # M117-M119 打 v2.3.36。M117 属于"把事实说错"那一类: 代码看着还能跑, 但逐 IP 的 wave 一旦
    # 只剩一个 max, "同节点跨档位分数变不变"就真的永远测不出来了 —— 而账上正挂着"4 路分摊"。
    ("M117", "screen_wave 只存一个 max (回退成『不是逐 IP』, 4 路分摊就真的测不了了)",
     "xbox-speedtest.py", "C93",
     '        "screen_wave": dict((k, v) for k, v in (screen_wave or {}).items() if v is not None),',
     '        "screen_wave": {"_max": max((screen_wave or {}).values() or [0])},   # 变异: 丢掉逐 IP'),
    ("M118", "status 不区分 created / created_after_writefail (上一轮写失败与首次部署同形)",
     "xbox-speedtest.py", "C86b",
     '        status = "created_after_writefail" if os.path.exists(seen_path) else "created"',
     '        status = "created"      # 变异: 与首次部署同形'),
    ("M119", "隔离文件不再加序号 (同一秒两次隔离, 前一份坏证据被覆盖)",
     "xbox-speedtest.py", "C94",
     '''        _bak, _n = _base_bak, 1
        while os.path.exists(_bak):
            _bak = "%s-%d" % (_base_bak, _n)
            _n += 1''',
     '''        _bak = _base_bak       # 变异: 同秒覆盖前一份坏证据'''),

    # M120-M121 打 v2.3.37。M120 就是 v2.3.36 的那一刻本身: 2c 把 Cn1/Cn2 混进同一张表,
    # 而 WIN_RATIO / DEGRADE_MIN_RECORD 在别处是按两组两个分布处理的 —— 一次 `--pool Cn2`
    # 的定向重测就能把整张表中位数挪一次。合成样本 Cn1 恒 10.0 / Cn2 恒 90.0, 混表会得到 ~50。
    # M121 是"修了但没人看见"这一类: 返回值分细了, 而 main 里 `_merged, _st = ...` 零读者。
    ("M120", "replay 2c 不分组 (Cn1 10.0 / Cn2 90.0 混表, wave=1 中位落到 ~50)",
     os.path.join("tests", "replay.py"), "C95",
     '        _g = _r.get("group") or "?"',
     '        _g = "mix"      # 变异: 两组混进同一张表(v2.3.36 的那一刻)'),
    ("M121", "created_after_writefail 不再打印 (status 分细了但日志里仍然同形)",
     "xbox-speedtest.py", "C86b",
     '        if status == "created_after_writefail":',
     '        if False and status == "created_after_writefail":   # 变异: 分了但不打'),

    # M122-M125 打 v2.3.38。
    # M122 就是 N1 那一刻本身: 2c 只按 (group, wave) 分桶、不读 `screen_need` —— 台账要求的那场
    # 跨配置实验的样本一进来, wave=1 的桶里就同时装着 W=1 的 76.0 与 W=4 的 22.0, 中位 49.0。
    # M123 是"两义合一": 完整率把老格式的缺席与新版本丢字段算进同一个 N。
    # M124 是"唯一承载判定理由的字段恰好不被检查": WANTED 少 `note`, 整列消失也全绿。
    # M125 是静默丢弃: 配不上的 IP 直接 continue, 真机 143 条就这么没了、一个字不说。
    ("M122", "2c 不按配置分桶 (screen_workers 丢掉: W=1 的 76.0 与 W=4 的 22.0 落进同一桶)",
     os.path.join("tests", "replay.py"), "C96",
     '        _w = int(_wo) if isinstance(_wo, (int, float)) and _wo > 0 else None',
     '        _w = None      # 变异: 配置指纹丢掉, 全落进同一桶(v2.3.37 的那一刻)'),
    ("M123", "字段完整率不看首现行 (老格式的缺席算成新版本丢字段)",
     os.path.join("tests", "replay.py"), "C97",
     '        _lack = [r for r in rows[_first:] if k not in r or r.get(k) is None]',
     '        _lack = [r for r in rows if k not in r or r.get(k) is None]   # 变异: 两义合一'),
    ("M124", "WANTED 删掉 note (唯一承载判定理由的字段, 整列消失也全绿)",
     os.path.join("tests", "replay.py"), "C98",
     '          "cur", "wr", "st_cur", "st_cand", "q_cur", "note",',
     '          "cur", "wr", "st_cur", "st_cand", "q_cur",   # 变异: note 不在清单里'),
    ("M125", "配不上的 IP 静默丢弃 (只有 screen 没有 wave 的条目不算、不报)",
     os.path.join("tests", "replay.py"), "C93",
     '                _c["unpair"] += 1',
     '                pass        # 变异: 静默丢弃(v2.3.37 的那一刻)'),

    # M126-M129 打 v2.3.39。
    # M126 就是 P1-A 的那一刻本身: 分桶键用 `screen_need`(派生量)而不是 `screen_workers` ——
    # 劣化夜"只活 3 个节点"被读成"另一种并发配置", 于是在**从未做过实验**的样本上凭空生出 Δ。
    # M127 是"缺席原因必须可辨"的第六遍漏在同一处: 行内写着 `no winner`, 检查不用。
    # M128 是沉默: need 的推导口径被改了(它只是派生量), 而画表照旧。
    # M129 是 P3-E: 一侧只有一次测量也照样把两个中位数相减、报一个百分号。
    ("M126", "配置指纹退回 screen_need (派生量当配置: 劣化夜被读成另一种并发, 凭空生出 Δ)",
     os.path.join("tests", "replay.py"), "C99",
     '        _g = _r.get("group") or "?"\n        _wo = _r.get("screen_workers")',
     '        _g = _r.get("group") or "?"\n'
     '        _wo = _r.get("screen_need")   # 变异: 把派生量当配置(v2.3.38 的那一刻)'),
    ("M127", "坏夜行(decision='no winner')被算成字段缺损 (8 个决策字段长期挂'缺 1 行')",
     os.path.join("tests", "replay.py"), "C100",
     '    if isinstance(_d, str) and _d.startswith("no winner") and k in DECISION_KEYS:',
     '    if False and isinstance(_d, str) and k in DECISION_KEYS:   # 变异: 坏夜当成丢字段'),
    ("M128", "need 与 80 × max(wave) 不符时沉默 (推导口径变了, 画表照旧)",
     os.path.join("tests", "replay.py"), "C101",
     '                if abs(_exp - _nd) > 1e-6:',
     '                if False:      # 变异: 口径变了不吭声'),
    ("M129", "跨配置 Δ 不看 n (一侧只有一次测量也报百分号)",
     os.path.join("tests", "replay.py"), "C96",
     '                _ok_n = all(n >= 2 for _, _, n in _series)',
     '                _ok_n = True   # 变异: 一测也报 Δ(v2.3.38 的那一刻)'),
    ("M130", "第六节真值不剔本轮 (拿答案对答案: Top1 命中 1/3 -> 2/3)",
     os.path.join("tests", "exp_screen_workers.py"), "C102",
     "            if j == i:\n                continue",
     "            if False and j == i:\n                continue"),
    ("M131", "_topk 改升序 (同一把尺子翻了: 名次与重合率变成排序实现的函数)",
     os.path.join("tests", "exp_screen_workers.py"), "C103",
     "key=lambda kv: (-kv[1], kv[0])", "key=lambda kv: (kv[1], kv[0])"),
    ("M132", "不足 3 轮也出召回结论 (两轮的中位就是一次测量)",
     os.path.join("tests", "exp_screen_workers.py"), "C104",
     "    if len(rs) < 3:", "    if False:   # 变异: 两轮也出结论"),
    ("M133", "wave 自证恒 OK (并发没生效的格照样进表)",
     os.path.join("tests", "exp_screen_workers.py"), "C105",
     '"OK" if got == [w] else', '"OK" if True else'),
    ("M134", "审计不删自己造的临时目录 (NAS /tmp 累积 23.8 万条 -> tmpfs 塞满, 自检卡在 ENOSPC)",
     "tests/audit.py", "D9",
     "        shutil.rmtree(_t, ignore_errors=True)",
     "        pass   # 变异: 不删(实测把 NAS 的 /tmp 塞满)"),
    ("M135", "第七节全部快测先跑 (逐 IP 反向没了: 秩相关含时间漂移, 是时钟的函数)",
     os.path.join("tests", "exp_screen_workers.py"), "C106",
     "        if k % 2 == 1:", "        if False:   # 变异: 全部快测先跑"),
    ("M136", "第七节秩相关把 0 值也算进来 (没测出被当成最慢参与排序)",
     os.path.join("tests", "exp_screen_workers.py"), "C107",
     'ok = [r["ip"] for r in rows if r["quick"] > 0 and r["steady"] > 0]',
     'ok = [r["ip"] for r in rows if r["quick"] > 0]   # 变异: 稳态 0 也进来'),
    ("M137", "from-json 配 stage steady 静默跳过 (不做事、不报错、还退出 0)",
     os.path.join("tests", "exp_screen_workers.py"), "C108",
     '        if a.stage in ("steady", "both"):',
     '        if False:   # 变异: 静默跳过第七节'),
]


def run_audit(d, extra_env=None, audit_path=None):
    env = dict(os.environ)
    env["XBOX_LEDGER"] = LEDGER      # 台账不在 FILES 里, 必须指回真身(否则 A18 只会报"台账不存在")
    if extra_env:
        env.update(extra_env)
    # v2.3.39 第四十轮: `audit_path` 是为了打**审计器自己**的变异(D9 的 M134)。
    # 不传就是真身 —— 那时"注入到 audit.py 的改动"根本不会被执行, 会以"漏网"收场。
    p = subprocess.run([sys.executable, audit_path or AUDIT, "--dir", d], env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def _copy_all(dst):
    """把本侧实际存在的交付物复制进变异目录, 缺的跳过(已在 MISSING 里公告过)。"""
    for f in FILES:
        p = os.path.join(SRC, f)
        if os.path.isfile(p):
            out = os.path.join(dst, f)
            od = os.path.dirname(out)
            if od and not os.path.isdir(od):
                os.makedirs(od)          # tests/replay.py 这种带子目录的交付物
            shutil.copy(p, out)

def main():
    tmp = tempfile.mkdtemp(prefix="mutant_")
    fails = []
    skipped = []
    try:
        if MISSING:
            # NAS 侧就是这样: deliver.sh 是本地推送工具, 不在 PAIRS 里, 那边没有这个文件。
            print("NOTE 本侧缺少交付物: %s | 打它的变异按 SKIP 记(不进分母)" % ",".join(MISSING))
        # 基线: 原样复制必须全过(否则后面所有判断都没意义)
        base = os.path.join(tmp, "pristine")
        os.makedirs(base)
        _copy_all(base)
        rc, out = run_audit(base)
        ok = rc == 0 and "全过" in out
        print(("PASS" if ok else "FAIL") + " 基线: 未经修改的交付物审计全过")
        if not ok:
            fails.append("baseline")
            print(out[-1500:])

        for mid, desc, fn, expect_cid, old, new in MUTANTS:
            if fn in MISSING:
                # 打的是本侧不存在的文件: 复制都复制不了, 更谈不上"改动后必须红"。
                # 记 SKIP 而不是 FAIL —— 缺文件是交付集合的侧别差异, 不是审计器的漏网。
                skipped.append(mid)
                print("SKIP %s %s | 本侧没有 %s(%s), 该变异只能在有它的那一侧跑"
                      % (mid, desc, fn, "NAS 侧不含本地推送工具"))
                continue
            if mid in ("M68", "M71", "M78") and sys.version_info < (3, 9):
                # 本机就是 py3.8 时, 这三条变异的"漂移"由解释器自己保证被发现:
                # M68 是**硬语法错误**(match 语句), A 段 load(pool_refresh) 先崩(A0/B0/C0 红);
                # M71 是**注解求值即炸**(3.8 里 list 不可下标), 同样崩在 import 那一行;
                # M78 同理(3.8 对 `int | None` 求值即 TypeError: unsupported operand)。
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
            _copy_all(d)
            if fn.replace("\\", "/").startswith("tests/"):
                # 打 tests/ 里那些文件的变异(比如 replay.WANTED), 必须把它们**放进变异目录** ——
                # audit 对 tests/* 默认走真实路径(见 A22 的注释), 不复制过来就等于改了个没人读的副本。
                # v2.3.39 第四十轮: 判断前先把 `\` 归一成 `/` —— `os.path.join` 在 Windows 上给的是
                # `tests\exp_screen_workers.py`, 直接用 `startswith("tests/")` 恒为假, 探针的三条变异
                # 会以 FileNotFoundError 收场(看起来像"注入点不存在", 实际是自检没把被审对象带进来)。
                # v2.3.37: 不能再无条件 makedirs —— FILES 里已经有 `tests/replay.py` 了, `_copy_all`
                # 会先把 `d/tests` 建出来, 这一行就从"建目录"变成 `FileExistsError`(实测 M103 当场崩,
                # 前面 102 条已经跑完, 后面 19 条一条没跑 —— 崩在这里看起来像"自检跑完了")。
                if not os.path.isdir(os.path.join(d, "tests")):
                    os.makedirs(os.path.join(d, "tests"))
                shutil.copy(os.path.join(HERE, os.path.basename(fn)),
                            os.path.join(d, "tests", os.path.basename(fn)))
            extra_env = {}
            if fn == "LEDGER" and mid == "M60":
                # 引用假账: 只把台账头部那处检查编号改错, 条目内容与排序**一个字节不动**。
                # 交付物(FILES 里那几个)也没动, 所以绿/红只可能来自 A19 —— 这条断言唯一诚实的验法。
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
                # 交付物(FILES 里那几个)一个字节不动, 所以红只可能来自 A18/A19 —— 这也是这些断言
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
            # 打审计器自己的变异(M134): `run_audit` 默认跑**真身**, 必须换成变异目录里那一份,
            # 否则注入的改动根本没被执行, 结果会以"漏网"收场 —— 看起来像断言不灵, 实际是改了个
            # 没人读的副本(和 A22 那个"文件不存在"是同一类)。同时把 XBOX_SELFTEST 指回真身:
            # 变异目录里没有 selftest_audit.py, A22 会凭这一点假红。
            _ap = None
            if fn.replace("\\", "/").endswith("tests/audit.py"):
                _ap = os.path.join(d, fn)
                extra_env.setdefault("XBOX_SELFTEST", os.path.join(HERE, "selftest_audit.py"))
            rc, out = run_audit(d, extra_env, _ap)
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
