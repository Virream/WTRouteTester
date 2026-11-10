# statshark 回放查看器(Replay Viewer) · 调查记录

> **调查时间**: 2026-11-10 · **样本**: 回放 `2b315cb70029ddda`(Abandoned Factory, `abandoned_factory_Conq2`,
> 时长 390 982 ms, 89 个单位, 32 名玩家)
> **方式**: 在 VS Code 内置浏览器里打开它, 只读地看网络请求 / JS 分块 / 接口响应。
> **不涉及任何游戏文件**, 未对站点做任何写操作。
>
> **一句话结论**: 它是「**服务端解析 + 客户端只做 three.js 渲染**」, 客户端拿到的是一份
> 预处理好的 60 MB 语义化 JSON 与一堆静态 glTF/DDS 资产 —— 所以"它把录像的每个字节都解析了"
> 是真的, 但那份工作量在服务器上, 不在浏览器里。

## 0. 结论速览

| 问题 | 结论 |
|---|---|
| 解析在哪跑? | **服务器端**(客户端只下载结果) |
| 是开源的吗? | **不是**: 无官方仓库、无文档、闭源 SPA + 闭源桌面端 SharkClient |
| 我们能不能照抄? | 代码不能; **口径与数据形状可以照**(见 §7/§8) |
| 它的 3D 从哪来? | 服务端**离线**从游戏资源包提取 → glTF + 原版 `.dds` 贴图 + 自定义 splat 材质 |
| 炮塔为什么和游戏一致? | 每帧样本里**直接带 `turretYaw` / `gunPitch`**, 不是自己算 |
| 出生/阵亡怎么定? | 出生 = 该单位**第一条采样**; 阵亡 = **击杀消息的时间戳**(1 ms 内精确匹配) |
| 对我们的意义 | **"自研全量回放解析"搁置**(§9); 但三条口径可以立刻用在现有导入逻辑上 |

## 1. 调查方法(可复现)

1. 打开任意一局回放页面;
2. 在页面里读 `performance.getEntriesByType('resource')` → 拿到全部请求与体积;
3. 把 73 个 JS 分块拉下来搜关键字(`.glb` / `vehicle.json` / `turretYaw` / `zstd` / `wrpl` …);
4. 用 `addInitScript` 在页面脚本之前挂 `XMLHttpRequest` 的 `open/send`, 在 `load` 事件里
   把 `/api/replay-viewer/by-id` 的响应解析成"结构摘要"(字段名、数组长度、样本条目);
5. 直接 `fetch` 静态资产(如 `models/veh/m1a1_abrams.glb` 与同名 `.vehicle.json`)核对内容。

> ⚠ 直接 `fetch('/api/replay-viewer/by-id')` 会 **406**(要携带站点自己的请求头),
> 所以走第 4 步的钩子拿响应, 而不是自己重建请求。

## 2. 技术栈

| 项 | 结论 |
|---|---|
| 前端 | Angular SPA(71 个分块) |
| 3D | **three.js r171**(`window.__THREE__ === '171'`) |
| 播放器逻辑 | `chunk-DdLWnyzq.js`(286 KB) |
| 地形材质 / DDS / GLTF / 道具 | `chunk-jwZhuHRw.js`(78 KB) |
| three.js 本体 | `chunk-DiEjlPk0.js`(539 KB) |
| 自带性能面板 | 100 fps / 10 ms 帧 / 3.4~7 M 三角形 / 900~1600 draw call |

## 3. 解析在服务器端 —— 五条硬证据

| 证据 | 说明 |
|---|---|
| 客户端只调 `POST /by-id` / `/by-url` / `/upload` | `/upload` 是 **multipart 把 `.wrpl` 文件传上去**; 回来的是语义化 JSON, 客户端再没见过原始包流 |
| 73 个 JS 分块里**没有任何解压实现** | 搜 `zstd` / `pako` / `inflate` / `DecompressionStream` / `oodle` **全部 0 命中** |
| `wrpl` 只出现在文案里 | `Choose a .wrpl file first` / `Only .wrpl files can be uploaded` / `A single .wrpl, under N MB` |
| 另有 `/build-status {level}` 与 `/hq-models {key}` | 按**关卡**构建地形/道具、按**车型**生成高质量模型 —— 资产管线也在服务端, 客户端只轮询进度 |
| 查看器要账号 | `REPLAY_VIEWER_AUTH_REQUIRED: Sign in with a supporter account to use the replay viewer` |

## 4. 接口与数据形状

```
POST /api/replay-viewer/by-id    {"sessionId":"..."}   → 60 MB JSON(gzip 约 5.4 MB)
POST /api/replay-viewer/by-url   {"url":"..."}
POST /api/replay-viewer/upload   multipart 字段 replay
POST /api/replay-viewer/build-status {"level":"..."}   → 关卡资产构建进度
POST /api/replay-viewer/hq-models    {"key":"..."}     → 按车型取高质量模型
```

`schemaVersion: 1` 的响应结构(**这就是我们该照着长的目标形状**):

| 字段 | 内容 |
|---|---|
| `frame` | `{units:"metres", up:"+Y", handedness:"right", angleUnits:"radians"}` |
| `units[89]` | `{id, team, playerName, vehicleName, kind:"ground"/"air", modelKey, loadout[], hasPov, camera{trackIds,samples[]}, detectedSpans, scoutedSpans, repairingSpans}` |
| `tracks[89]` | `{unitId, team, spawnMs, deathMs, modelKey, samples[{timeMs, x, y, z, heading, pitch, roll, **turretYaw**, **gunPitch**, hasTurret, lockTargetId, radarMode, ammoRemaining, aoaDeg, povYaw, povPitch, povDistance, aimRange}], targetSamples}` |
| `projectiles` / `events` / `rounds` / `counterMeasures` / `smokes` / `shots` / `fireBursts` / `tracers` | 43 / 498 / 113 / 62 / 41 / 104 / 842 / 2782 条 |
| `terrain` | `{meshUrl, positions, indices, mapImageUrl, waterLevel}` |
| `models[40]` | `{key:"m1a1_abrams", url:"models/veh/m1a1_abrams.glb", scale:1}` |
| `zones[1]` | `{name, label:"A", x, y, z, radius, radiusIsDefault, startOwner, states[{timeMs, team1, team2, owner, status, progress, actor}]}` |
| 其它 | `scoreboard[32]`、`missionObjects[1090]`、`mission`、`environment{timeOfDay, weather, latitude, longitude, date, estimated, samples}`、`airfields` |

## 5. 静态资产(`/api/replay-viewer/assets/`, 同一局约 73 MB)

```
levels/avg_abandoned_factory/
  terrain.glb  terrain-outer.glb            地形网格(43 MB 那一组)
  props.mesh.bin  props.inst.bin            道具网格 + 实例变换
  decals.bin  combined.bin                  贴花 / 合并缓冲
  terrain-material.json  props.json         材质参数、道具清单
  weights0.png weights1.png biome_index.png 地形混合权重贴图
  tex/*.dds (59)  proptex/*.dds (68)        **游戏原版 DDS 贴图**(不转格式)

models/veh/m1a1_abrams.glb                  载具模型(488 KB, model/gltf-binary)
models/veh/m1a1_abrams.vehicle.json         炮塔/火炮挂点(333 B, 自定义)
```

`m1a1_abrams.vehicle.json` 全文:

```json
{"schema":1,"space":"scene",
 "turret":{"node":"turret","pivot":[0.0348,1.5296,-0.0031],"axis":[0,1,0]},
 "gun":{"node":"gun","pivot":[1.2431,1.8923,0.0199],"axis":[0,0,1]}}
```

## 6. 两个"为什么像游戏"的机制

- **炮塔**: 每帧样本带 `turretYaw` / `gunPitch`(弧度), 客户端把它们分别绕 `turret.axis` /
  `gun.axis` 施加到对应 node。加载流程 `loadModelGroups(url)` = `loadGltf(url)` +
  `loadTurretRig(url)`(把 `.glb` 换成 `.vehicle.json`); 拿不到就退回静态模型
  (日志 `turret sidecar failed; model will not rotate`)。
- **地形**: `terrain-material.json` + 自定义 GLSL, 源码注释原话 ——
  > `terrain.glb is at scene identity, so modelMatrix*position is scene space;
  > recover Dagor worldZ by un-flipping z to sample the atlases in the ...`

  即**复刻 Dagor 引擎的地形 splat 混合**(权重贴图 × 纹理图集)。
- 图标走公开镜像: `cdn.jsdelivr.net/gh/gszabi99/War-Thunder-Datamine@master/atlases.vromfs.bin_u/units/<name>.png`

## 7. 出生 / 阵亡口径(我们绕了半天的答案)

同一局 89 个单位的实测:

| 观测 | 数值 | 含义 |
|---|---|---|
| `spawnMs` vs 第一条采样 `timeMs` | **89/89 = 0 ms** | 出生 = 该单位第一条位置采样,**没有出生包** |
| `deathMs` vs 最后一条采样 | −1660 ~ **−5000** ms(中位 −5000) | 残骸死后仍在上报 → 阵亡**不能**从采样尾巴推 |
| `kill` 事件 ↔ `deathMs` | **58 / 58 在 1 ms 内相等** | 阵亡 = **击杀消息的时间戳**, 原样使用 |
| 无 `deathMs` | 31 / 89 | 结束时还活着 |
| `events` 种类 | `kill` 58 / `critical_damage` 31 / `award` 405 / `severe_damage` 3 / `weapon_destroyed` 1 | **没有 `spawn` 事件** |
| 每 unit 的 track 数 | 每个恰好 **1** 条 | 一条命 = 一个 ECS 实体 id |
| `scoreboard[].unitIds` | 如 `["189"]` | 玩家 → 命 的映射来自**结算表**, 不靠推断 |

> 存疑未解: 它的 `scoreboard[].userId` 是**数字**(`212610145`), 我们解出来是空串。
> 可能读的是服务器录像的另一块, 也可能来自 WT Live 接口 —— 先不当成"我们解析错了"。

## 8. 能复用 / 不能复用

**能用的(口径与形状)**

1. **阵亡只认击杀消息的毫秒时间戳**; 残骸尾巴只做**显示层截断**(它保留约 5 s);
2. **一条命 = 一个有采样的 uid** —— 我们那套"2 s 窗口 + 寿命 ≥5 s"的启发式可以删掉;
3. **玩家 → 命 的映射用结算表**(我们已有 results BLK 的 32 人表);
4. **不要再找"出生包"** —— 它没有 spawn 事件, `probe_coverage.py` 那条线可判定"不需要";
5. **schema 当目标规格**: 我们已有 `x/y/z/heading/pitch/roll`, 缺 `turretYaw`/`gunPitch`、
   `detected/scouted/repairing` 三类 span、弹药与曳光弹。

**不能用的**

- 代码(闭源)、资产(游戏版权, 自用无妨但**不要进公开仓库**);
- 它的"服务端"我们不需要模仿 —— 本机就是服务端, `replay_import → GlobalRecord` 已是这条链路,
  且我们的列式二进制(`.wts`)比 60 MB JSON 更适合存储。

## 9. 项目决定: **自研全量回放解析 —— 搁置**(用户 2026-11-10)

理由(用户原话的意思): **只依靠上游开源项目是远远不够的**, 要把它做到 statshark 那个程度
(炮塔角、点亮/侦察/修理区间、弹药、曳光弹、载具模型与地形资产), 就**必须逆向游戏文件**;
而这条线受仓库最高优先级约定(§0)限制、风险与收益也不匹配。

因此:

- **不再往"全量 3D 回放"方向投入**; `docs/录像解析调研.md` 里那条"路线建议"停在当前位置;
- 我们真正需要的三样 —— **地面单位位置轨迹、击杀时刻、结算表** —— 已经拿到, 够支撑
  跑图线路测试器与赛道计时(见 `赛道检查点设想.md`);
- 若以后重启, 起点是这份文档 + `录像解析调研.md` §2.2 的三个开源实现, **不要从零再猜一遍**。

## 10. 参考

- 站点: <https://statshark.net/> (回放查看器要 supporter 账号)
- 开源路线(地形/3D 可参考): `ShioUmiusi/WT-MAP-ANALYZE`、`ShioUmiusi/WTACRP`
- 解析实现参考: `LivingTheDagor/WrplReplayParser`、`maxsupermanhd/wrpl-inspector`、
  `ShioUmiusi/WTACRP` 的 `docs/REPLAY-FORMAT.md`
- 相关记录: [`录像解析调研.md`](录像解析调研.md) §2.8 / §2.8.1 / §2.9
