# WTRouteTester · 跑图线路测试器

**v1.1.0**(2026-10-22)

记录「重生后多久到达某位置」的小工具:在战争雷霆(War Thunder)里从重生点出发录一条路线,
之后就能看到自己(或队友)到达每个标记点用了多久。

- 只读取游戏自带的 **8111 本地接口**(游戏官方提供,默认开启),不注入游戏进程、
  不读写游戏内存、不读写游戏文件;
- 数据全部存在本地(程序自己的 `data\` 目录),不上传任何东西;
- 依赖只有 **PyQt6** 与 **requests**;想要 GPU 地图画布再加一份 `requirements-gl.txt`
  (pygfx/wgpu,只装前两个时自动走软件渲染)。

## 功能

- **录制路线**:开始 → 途中按标记键打点 → 结束,自动记录每个点的用时(相对重生时刻);
- **全局录制(整局所有玩家)**:回放里把整局录两遍(A 队 / B 队 各一次),
  侧栏“存放路线的那一级”会多出一个以录制时间命名的条目,展开分 A/B 两队、组内按时间正序;
  只录地面单位(含微型侦察无人机), 录制期间锁定地面地图;
- **地图与坐标**:读取 8111 的 `/map.img` 与 `/map_obj.json`,把轨迹画在地图上,
  地图图片按内容哈希缓存(切换地图才重新下载);当前车辆画成一个圈,
  圈**前方**还有一个 `^` 跟着 `dx`/`dy` 实时转动 —— 一眼看出车头朝哪;
- **分组管理**:地图 → 游戏模式 → 版本(战区布局) → 轨迹 四层归类,支持改名、删除、显示/隐藏;
- **回放对比**:多选轨迹一起回放,按速度着色,一眼看出谁在哪一段更快;
- **全局快捷键**:游戏中也能按(F5 开始 / F6 标记 / F7 结束,可改);程序**不拦截按键**。
- **阵亡时刻修正**(v1.1.0):每队只有“摄像机挂着那位玩家”的死亡时刻拿不到(8111 不会
  立刻让他的 icon 消失)。条目上的**黄色感叹号**就是提醒这个:点一下,从本局击毁名单里
  点选 A/B 队各自观战的那位,程序用 `/hudmsg` 把真实阵亡时刻补回来 —— 一局死几次就修
  几条线。名单里 **★** = 每一条命都对上了,**✔** = 能修其中几段,灰色 = 对不上。
- **录制叠加层**(v1.1.0):录制时在屏幕角落显示**实际采样率**、轮询频率、点数、标记数
  (菜单「设置 → 叠加层」可选位置/不透明度/字号)。
- **颜色约定**:阵亡叉与回放光标点的颜色跟**该队重生点在地图上的颜色**一致;
  多队同屏时一眼能分出阵营。

## 运行(源码)

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
# 可选: GPU 地图画布(不装则自动用软件渲染)
.venv\Scripts\python.exe -m pip install -r requirements-gl.txt
.venv\Scripts\python.exe route_tester.py
```

## 测试(离屏, 不需要开游戏)

```powershell
.venv\Scripts\python.exe tests\route_tester_test.py
.venv\Scripts\python.exe tests\global_record_test.py
```

覆盖存储 / 录制状态机 / 地图渲染 / 回放 / 快捷键等,结尾会打印「全部通过: N 项」。
渲染类用例会把窗口画进内存位图再数像素 —— Qt 参数类型、图标溢出、朝向画到另一侧
这类缺陷只在真画一遍时才会暴露, 只做实例化的测试抳不到。

## 打包

```powershell
.\build_route_tester.ps1              # 默认只出 onedir 目录版(启动快, 推荐)
.\build_route_tester.ps1 -OneFile     # 需要单文件分发时再加
```

产物在 `dist\WTRouteTester\`(整个文件夹拷走即可运行)。脚本会先把 `data\` 暂存,
打包完成后原样搬回 —— **录制的轨迹与地图缓存不会丢**。

发布时把整个目录压成 `dist\WTRouteTester_<版本>_win64.zip`(不含 `data\`,
首次运行会自动建), 版本号在 `wt81111g/config.py` 的 `ROUTE_VERSION` 与
`version_info_route.txt` 两处同步。

## 许可

Apache License 2.0(见 `LICENSE`);再分发请保留 `NOTICE`(作者信息与第三方组件说明)。

作者:**切真Viream** · [B站](https://space.bilibili.com/140035845) · [GitHub](https://github.com/Virream)
