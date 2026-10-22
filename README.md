# WTRouteTester — 战争雷霆 · 跑图线路测试器

基于战争雷霆官方 **8111 本地 HTTP 接口** 的小工具: 记录"重生后多久到达某位置"的线路,
多条对比、按绝对时间轴回放; 也可以整局**记录所有玩家的地面线路**(A/B 两队分别录一次),
用来复盘谁在什么时间走了哪条路。

> 与 [WTBlackList 黑名单助手](https://github.com/Virream/WTBlackLists) 是**两个独立程序**,
> 可以同时开、互不影响; 唯一共用的是"怎么调 8111"这套经验。

当前版本 **v1.1.1**(2026-10-22), 更新日志见 [`CHANGELOG.md`](CHANGELOG.md)。

## 下载

到 [Releases](https://github.com/Virream/WTRouteTester/releases) 下
`WTRouteTester_<版本>_win64.zip`, **解压即用**(无需安装)。

- 程序把数据写在 exe 同级的 `data\`(`routes.json` 录制的线路 + `maps\` 地图缓存);
  zip 里**不含** `data\`, 首次运行自动创建;
- 升级时把旧的 `data\` 一起拷进新目录即可, 录制内容不会丢。

## 主要功能

- **单条线路录制**: 25Hz 采样, 按速度配色, 可打时间标记, 算转弯半径;
- **地图与战区**: 直接取 8111 的地图图片(自动识别同图不同分辨率)、战区(A/B/C)与重生点;
  关掉游戏也能用缓存看回放;
- **多条对比**: 侧栏按 地图 → 模式 → 版本(战区布局) → 线路 四层分组,
  眼睛图标控制显示, 选中即回放;
- **A/B 队全局录制**: 两次独立观战分别录下整局所有玩家的地面线路, 分两队按绝对时间轴
  同步回放; 支持边录边画、重录覆盖;
- **阵亡时刻修正**: 用 `/hudmsg` 的击毁记录, 把"摄像机挂着那位玩家"的真实阵亡时刻补回来
  (一局死几次就修几条线), 阵亡处画叉、线路变暗;
- **录制叠加层**: 录制时显示实际采样率、轮询频率、点数、标记数;
- **GPU 画布**: 装了 `pygfx` + `wgpu` 时用 GPU 画(见 `requirements-gl.txt`),
  否则自动回退软件渲染。

## 数据来源与反作弊

只读游戏自带的 `http://127.0.0.1:8111` 本地接口,**不读取、不修改任何游戏文件**
(不碰安装目录、日志、存档), 不注入进程 —— 纯外部轮询。

## 快捷键(默认)

| 键 | 动作 |
|---|---|
| `F5` | 开始 / 结束录制 |
| `F6` | 打一个时间标记 |
| `F7` | 结束录制并保存 |

可在菜单「快捷键」里改; 程序不拦截按键(游戏也会收到), 建议绑游戏里没用的键。

## 从源码运行 / 打包

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt          # GPU 画布另加 requirements-gl.txt
.venv\Scripts\python route_tester.py                   # 直接运行

.\build_route_tester.ps1                               # 打包 onedir 目录版
```

打包前请**关掉正在运行的程序**, 否则 `dist\WTRouteTester\data` 被占用会中止
(脚本会保护该目录, 绝不删用户的录制数据)。

## 文档

| 文件 | 内容 |
|---|---|
| [`docs/WTRouteTester-FEATURES.md`](docs/WTRouteTester-FEATURES.md) | 功能与实现总览(界面/状态模型/回放/全局录制/打包) |
| [`docs/跑图线路测试器.md`](docs/跑图线路测试器.md) | 设计细节(布局、快捷键、绘制规则、8111 数据来源、性能与踩坑记录) |
| [`docs/8111使用经验.md`](docs/8111使用经验.md) | 8111 接口实测经验(端点/玩家坐标/地图/hudmsg 格式) |
| [`CHANGELOG.md`](CHANGELOG.md) | 更新日志 |

## 许可

Apache License 2.0, 见 [`LICENSE`](LICENSE); 第三方组件与免责声明见 [`NOTICE`](NOTICE)。
