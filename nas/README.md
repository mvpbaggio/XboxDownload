# NAS 全自动测速钉 IP 方案（XboxDownload 的无人值守超集）

本目录是 [XboxDownload](https://github.com/skydevil88/XboxDownload) 主项目 README 第 3 节「不想开电脑下载」路线的**全自动实现**：上游方案是「路由器写 hosts 钉 IP」但需手动测速手动填；本方案在 NAS 上跑 Python 工具链，把「测速 → 选最快 IP → 写路由器 → 死亡看护 → 热备止血」整条链路无人值守化。

Xbox 主机侧**零改动**：DNS 解析被路由器 dnsmasq 钉到最快 IP，下载自动走最快线路。

## 架构

```
┌─ NAS (python3.8+, cron) ─────────────────────────────┐
│  xbox-speedtest.py  每日 04:00 全量测速               │
│    1. 区域优先 8MB 粗筛(并发4) → Top8                 │
│    2. 80MB 快测排序 + 当前IP/区域保送候选             │
│    3. 冠军 vs 当前IP 8s 稳态复测, 赢迟滞阈值才换      │
│    4. uci 幂等写 dnsmasq address + restart            │
│    5. results.json 附 hotspares 热备名单              │
│  watchdog.py        每小时 :30 体检                   │
│    死/慢(<8MiB/s) → ① 热备止血(分钟级切次优)          │
│                    ② 触发 --pool 定向重测(只测坏组)   │
│  pool_refresh.py    手动: DNS 解析追加新边缘节点      │
└──────────────┬───────────────────────────────────────┘
               │ ssh (免密, ~/.ssh/id_ed25519_router)
        ┌──────▼──────┐         ┌──────────┐
        │ OpenWrt 路由 │ dnsmasq │  Xbox    │
        │ 钉 CN 域名IP │────────▶│ 自动走   │
        └─────────────┘         │ 最快线路 │
                                └──────────┘
```

## 与上游手动方案的关键差异

| 能力 | 上游 README 方案 | 本目录 |
|---|---|---|
| 选节点 | 手动测速手动填 | 三段测速自动选（粗筛→快测→稳态复测） |
| 节点死亡 | 无感知，下载变龟速才人肉发现 | 看门狗每小时体检，分钟级热备止血 |
| 防抖动 | 无 | 分组迟滞（Cn1 稳赢 15% / Cn2 稳赢 30% 才换，防夜间 CDN 波动频繁翻转） |
| 龟速节点 | 全量逐个测，可能拖数小时 | 25s 墙钟上限 + 区域优先粗筛，全轮约 10 分钟 |
| 幂等性 | 手写 hosts 易堆积 | uci del_list→add_list 幂等更新，重启不堆积 |

## 文件

- `xbox-speedtest.py` — 测速主程序（v2.3.4）。用法：`python3 xbox-speedtest.py [--pool Cn1]`
- `watchdog.py` — 看门狗（v2.3.4）。用法：`python3 watchdog.py`
- `pool_refresh.py` — IP 池 DNS 刷新。用法：`python3 pool_refresh.py`（目录自动取自脚本位置；写入逻辑在 `main()` 内，被 import 无副作用）
- `tests/audit.py` — **交付前机器审计**（见下节），`python3 tests/audit.py` 全绿才允许部署
- `tests/selftest_audit.py` — 审计器自检（变异测试：注入 6 类已知缺陷，断言审计必须变红）

## 交付前自审（机器断言，不是人眼）

改完代码先跑，两条都过再碰 NAS：

```
python3 tests/audit.py            # 全绿才允许部署(条数以实际输出为准)
python3 tests/selftest_audit.py   # 6 个变异体必须全被抓到
```

需要连自己真实凭据一起查时，用环境变量传入（**不要写进仓库**）：

```
XBOX_AUDIT_DENY="我的NAS密码,我的手机号" python3 tests/audit.py
```

审计分四层：

| 层 | 内容 | 拦的是哪类事故 |
|---|---|---|
| A 静态契约 | 组名/域名集合三处一致（`POOLS[4]`≡`DOMAINS`≡`pool_refresh`）、测速对一致、README 常量与代码一致、版本号一致、环境变量双向对齐、死函数、未用导入、锁文件不冲突 | v2.3.3 的「止血只切半个组」就是这一层漏的 |
| B 失败出口棘轮 | AST 统计每个函数的 `return False/None/0.0` + `except` 条数，与登记表比对；新增失败路径立即红，强制补用例 | 「修完又冒新问题」——未覆盖的失败路径变成会自鸣的数字 |
| C 行为契约 | 分类五档、采样抖动复测、整组止血、幂等、restart 失败不谎报、热备分散+下限+排序、预算截断不 KeyError、四个决策分支、`--pool` 合并落盘 | 逻辑回归（改 A 处弄坏 B 处） |
| D 交付卫生 | 凭据/私网指纹/个人路径（通用特征 + 本地私密清单）、CRLF、README 完整性、utf-8 声明 | 把密码或内网结构推到公网仓库 |

B 层登记表在 `tests/audit.py` 顶部（`FAILURE_EXITS` / `FAILURE_TESTS`）。**改了失败路径不更新它，审计会红**——这是刻意的。


## 部署

1. NAS 装 python3.8+，本目录放如 `/opt/xbox-speedtest/`
2. NAS→路由器配 ssh 免密：`ssh-keygen -t ed25519 && ssh-copy-id root@<你的路由器内网IP>`
3. IP 池文件 `IP.XboxCn1.txt` / `IP.XboxCn2.txt`（格式：`IP<TAB>(地区 运营商)`，可从上游 `IP/IP.AkamaiV2.txt` 或本地 `dig` 采集）
4. crontab（`<你的路由器IP>` 换成实际地址）：
   ```
   0  4 * * *  cd /opt/xbox-speedtest && XBOX_ROUTER_IP=<你的路由器IP> python3 xbox-speedtest.py >> run.log 2>&1
   30 * * * *  cd /opt/xbox-speedtest && XBOX_ROUTER_IP=<你的路由器IP> python3 watchdog.py >> watchdog.log 2>&1
   ```
5. 路由器侧需配套 URL 重写（302 com→cn），见主项目 [README_OpenWrt.md](https://github.com/skydevil88/XboxDownload/blob/master/README_OpenWrt.md)（lighttpd/Nginx 均可）

## 个性化配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `XBOX_ROUTER_IP` | `192.168.1.1` | 路由器内网 IP，**必须改成你自己的** |
| `XBOX_SCREEN_REGIONS` | `广东,广西` | 区域优先粗筛的省份列表，逗号分隔，按你所在地区配置 |

crontab 示例见上（部署第 4 步）。两个变量都可省略走默认值，但 `XBOX_ROUTER_IP` 默认 `192.168.1.1` 只是占位，**不设会连不上路由器**。

## 已知局限

- 测速文件 ≈ 游戏下载体验有误差：CDN 可能「只掐游戏内容不掐测速文件」（主机脉冲式卡顿需人工切冷节点）
- Cn2 测速 URL 是写死的版本化安装包路径，微软下架该版本后 Cn2 组测量失效（watchdog 健康体检已用 Cn1 文件规避此坑，speedtest 侧待跟进）
- 路由器 IP 走环境变量 `XBOX_ROUTER_IP`；ssh key 路径写死在脚本头部 `SSH_KEY`（`~/.ssh/id_ed25519_router`），换环境需改这一行
- watchdog 的 `POOLS` 是 3 元组、speedtest 的是 5 元组，两处结构不同，改动时注意解包（v2.3 曾因此崩溃）
- **每组要切的域名有两个来源**：speedtest 的 `POOLS[i][4]`（钉哪些）与 watchdog 的 `DOMAINS`（止血时一起切哪些）。两处必须一致，否则止血只切一半域名、主机仍命中死 IP（v2.3.3 修复项）；改动任一处请同步另一处
- 热备分散只能「尽量」：若当下只有一家运营商的节点可达（实测 2026-11-06 晚间广东电信段整体掉到 1MiB/s），名单仍会集中在同一家 —— 这是网络实况，不是代码问题

## 版本历史

- v2.3.4 交付流程整改：加入 `tests/audit.py`（分层机器断言）+ `tests/selftest_audit.py`（变异自检），此前「人眼审一遍」的方式不可复现、每被叫一次就冒一个新问题；顺带修 `pool_refresh.py` 无 `__main__` 保护（任何 import 都会真的解析 DNS 并改池文件）
- v2.3.3 交付审计修复：①止血只切首域名（assets2/d1/d2 仍指向死节点，日志却报"止血成功"）→ 整组域名一起切，并顺带扫掉仍指向死 IP 的漂移域名；②`dnsmasq restart` 失败时不再返回成功，交下一个热备/定向重测接手；③单次 8s 采样抖动就判死/劣化会误切（实测 108 的节点偶发掉到 40）→ 非 ok 时复测取高值，两次都低才动手；④清理死函数 `steady_top`、`pool_refresh` 显式 utf-8
- v2.3.2 快测双封顶（`MAX_QUICK_CANDS=16` 按粗筛分截断 + `QUICK_BUDGET_SEC=420` 总预算，原 31 个候选实测烧到 1496s）；热备名单按运营商分散（同运营商段级故障时整份热备会一起死）+ 20MiB/s 可用性下限；watchdog 加相对劣化判定（掉到记录值 55% 就动手，原绝对阈值 8MiB/s 让用户半速跑了两小时）
- v2.3.1 路由器 IP/省份改环境变量（去隐私化，个人配置不进仓库）；修 watchdog `POOLS` 3 元组解包崩溃（每小时体检全崩）；`pool_refresh` 目录自动定位
- v2.3 墙钟上限（25s）防龟速节点长跑；运行锁 ImportError/锁占用分离；dnsmasq restart 失败不再静默
- v2.2 修 `--pool` 定向重测覆盖另一组热备名单（按组合并+原子写）；池刷新标签改 `autoMMDD` 正则（原硬编码日期次日即失效）；路由器不可达不再白跑全量
- v2.1 移除网段黑名单（谁快用谁，节点死亡由热备止血兜底）
- v2 区域优先粗筛、分组迟滞、热备名单、`--pool` 定向重测
- v1 基础版：三段测速 + uci 幂等更新 + 运行锁
