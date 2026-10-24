<h1 style="font-size: 48px;">别倒卖了😭</h1>

# NekoSportsWorldTool

![Release](https://img.shields.io/github/v/release/YanamiNeko/NekoSportsWorldTool)
![Build](https://github.com/YanamiNeko/NekoSportsWorldTool/actions/workflows/release.yml/badge.svg)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-blue)

运动世界校园版全协议自动化工具，纯 Rust 单文件，GUI / CLI 双模式。

> ⚠️ 仅供学习研究，请勿用于违反学校规定的场景，后果自负。

## 功能

- **跑步**：一键全链（策略 → 打卡点 → 轨迹 → 提交 → OBS → 验证），轨迹按打卡点自然生成，开始时间支持随机或指定到前 3 天
- **AI 运动**：任务 / 自由练习双模式，批量补签（前 60 天 × 多项目）
- **数据**：学期完成度、违规自查、排行榜（个人/班级/院系 × 日/月、室内榜、历史榜）、个人主页
- **登录**：支持人机验证自动通过
- **自动更新**：桌面端检查/下载/自替换并重启（GitHub Release 直连，无镜像）；Android 下载 APK 调起系统安装器；启动检查模式可选（静默/询问/关闭）

## 界面

![GUI](docs/screenshot.png)

## 技术特点

- 完整实现 NetSecKit 信封加密与响应解密，每个响应强制 RSA 验签（fail-closed）
- 人机验证全程本地完成，不依赖任何外部服务
- 轨迹生成器：打卡点拟合环 + 自然速度曲线 + 哨兵点 / 断崖 / 点位吸附
- 设备身份持久化（DeviceId / 安装时间 / MAC），稳定不易触发风控
- GUI 与 CLI 共用同一套协议层，数据文件互通

## 使用

双击 exe 进入 GUI；CLI 用法：

```text
NekoSportsWorldTool login --user <手机号> --pass <密码> --remember
NekoSportsWorldTool policy                               # 只读查询跑步策略与当前规则
NekoSportsWorldTool fences                               # 只读查询学校围栏和区域编号
NekoSportsWorldTool points                               # 只读查询当前定位锚点的打卡点
NekoSportsWorldTool run                                  # 一键跑步（--altitude 17.2 固定海拔，或 11.6-22.8 映射到区间）
NekoSportsWorldTool ai --sport 2 --score 26000           # AI 运动
NekoSportsWorldTool rank main --type 1 --sort 1          # 排行榜
NekoSportsWorldTool update [--check]           # 自动更新（--check 仅检查不下载）
NekoSportsWorldTool template --file run.gpx   # 本地分析真实记录海拔（不会上传）
NekoSportsWorldTool help                                 # 全部命令
```

`template` 只读取用户手动选择的本地 GPX/JSON 文件，输出采样点、海拔范围、累计上升和累计下降，
不会登录、访问服务器或把模板记录接入跑步上传流程。

`policy` 使用已有会话读取学校跑步策略，输出北京时间、接口路径及规则响应；未登录时沿用已保存凭据的自动登录逻辑。它不生成或提交跑步记录，适合排查时间限制。Linux 调试构建可运行 `./target/debug/nekosportsworldtool policy`。

`fences` 查询学校服务端围栏；返回空列表时不能据此确认校园边界，也不能把一个定位锚点当作全程在校内的保证。

`scripts/policy_probe.py` 用于一次性定时任务：使用私有目录中的固定程序副本与会话，按 `schedule.json` 的北京时间执行，只允许在预约后的五分钟内启动请求。默认只读策略和围栏；会话失效时可使用 `login-once.json` 重登一次，完成后删除该凭据文件。用 `--state-dir <私有任务目录> --preflight` 检查文件而不联网。任务目录应放在仓库外，权限为 `0700`，凭据及运行数据权限为 `0600`。

显式配置 `submission_plan_sha256`（`run-plan.json` 的 SHA-256）及 `max_submissions: 1` 后，策略和围栏读取成功才会调用一次 `run-planned --file <计划文件> --execute`。计划与程序都校验哈希；任务启动标记持久化后不会再次执行。结果写入 `result.json`，保留策略、点位、轨迹、提交日志和回执。`record_submitted: null` 表示结果未知，禁止盲目重试；`record_submitted: true` 与服务端 `verification.complete` 达标判定分别记录。

在 `schedule.json` 中设置 `results_dir` 为工作区文件夹的绝对路径，可在任务结束时自动复制结果、日志、轨迹和回执；登录凭据、设备身份与会话文件不会复制。`--state-dir <私有任务目录> --export-results` 可离线同步已有结果。

`run-planned` 的计划字段见 `src/api/planned.rs::Plan`：时间均为真实北京时间对应的毫秒时间戳，包含预约、跑步开始及学校开放时段；距离 1000–3000 米、时长 300–1800 秒，配速仍须在 3–10 分钟/公里内。例如 2 公里、7:16/公里对应 `duration_s: 872`。`height_cm` 仅作步长检查，不写入海拔或账户资料。配置 BD-09 锚点、区域半径、步频与随机种子。运行前会验证最终轨迹、策略必经点、固定打卡点及服务端提供的围栏；可选打卡点按实际经过情况上报。空围栏不能证明路线全程在校内。

```bash
./target/debug/nekosportsworldtool run-planned --file /private/run-plan.json --check
./target/debug/nekosportsworldtool run-planned --file /private/run-plan.json --preview-points /private/points.json
```

以上两种检查均不联网、不提交；离线预览不代表服务器已确认规则。`--execute` 才运行真实流程，且仅在预约后的五分钟内允许提交。定时进程应配置 `TZ=Asia/Shanghai` 和 `UMask=0077`。

### 每日定时测试

`scripts/daily_runner.py` 每个北京时间日期只生成一份持久化计划：距离随机 2000–3000 米，开始时间随机 06:00:00–06:59:59，配速随机 6–9 分钟/公里，并排除时长不足 841 秒的组合。预计结束一分钟后进入提交窗口；systemd 每分钟检查一次，未到时间时不联网，错过五分钟窗口则当天跳过，不补交。`started.json` 和提交回执共同防止重复提交。

计划设置 `randomize_optional_points: true`、`min_ordinary_points: 2`，从当天服务器点位中随机抽取普通点，保留所有固定点及策略必经点，并检查最终轨迹实际经过的数量。身高用于估算参考步频，生成值不是实测数据。规则、点位或围栏校验失败时停止当天提交。

将 `daily_runner.py`、`policy_probe.py`、`weather_guard.py`、固定版本程序和设备身份模板部署到仓库外的私有目录 `~/.local/state/nekosports-daily/`。`settings.json` 必须包含 `first_date`、`binary`、`binary_sha256`、`identity_template`、`credentials_env`、`results_root`、`height_cm`、`anchor_lat_bd`、`anchor_lon_bd`、`max_radius_m`，以及 `weather` 对象中的 `latitude_wgs84`、`longitude_wgs84` 和可选的 `name`。天气坐标须转换为 WGS84，不能直接填写跑步使用的 BD-09 坐标。结果按日期保存；不要把实际配置、身份、凭据或结果提交到 Git。

每天到提交窗口后、登录校园跑之前，查询 [Open-Meteo 当前天气](https://open-meteo.com/en/docs)。该数据采用约15分钟粒度的天气模型；检测到降水量大于0或毛毛雨、雨、阵雨、雷暴天气代码时，记录 `skipped_rain`，当天不再登录、提交或重试。接口失败、数据缺失、时间超过30分钟或位置不符时记录 `skipped_weather_unavailable`，也跳过当天。次日重新查询。天气响应及判断保存在 `weather.json` 和 `result.json`，并导出到当天结果目录；查询只发送天气位置，不发送校园跑账号或凭据。

凭据文件仅允许 `NEKOSPORTS_USERNAME` 和 `NEKOSPORTS_PASSWORD` 两个赋值，支持可选的 `export` 前缀，权限必须为 `0600`；解析时不会执行 shell。调度器使用 `login --env`，密码不会出现在命令行参数或登录日志文件里。每次执行结束（包括失败）调用服务端登出并清除本地会话；`result.json` 的 `logout.server_confirmed` 单独记录服务器是否确认。异常终止后的清理由 `ExecStopPost` 执行，长期凭据文件保留供下一天登录。

配置完成后，将 `scripts/systemd/nekosports-daily.{service,timer}` 复制到 `~/.config/systemd/user/`：

```bash
systemctl --user daemon-reload
systemctl --user enable --now nekosports-daily.timer
systemctl --user list-timers nekosports-daily.timer
# 暂停后续任务
systemctl --user disable --now nekosports-daily.timer
```

服务需要主机在晨间保持开机、唤醒并联网。`--prepare-date YYYY-MM-DD` 只生成并检查计划，不登录或提交；`--cleanup-today` 只处理已启动任务遗留的会话。

手动海拔支持三种状态：留空使用自动海拔；填写单个数字（例如 `17.2`）让所有轨迹点使用固定海拔；填写区间（例如 `11.6-22.8`）将生成的海拔曲线映射到该上下限内，同时保留轨迹的相对起伏。

青龙定时任务：首次手工 `login --remember` 一次，之后定时挂 `run` 即可。

## 常见问题

- **提示未登录 / 401**：先执行 `login --remember`（GUI 勾选记住密码等价），会话失效会自动重登
- **10121 设备风险**：设备身份是持久化的，删除 `identity.json` 等于换了新设备，不要频繁删
- **10603 点位限流**：点位接口 5 分钟限 3 次，程序内置 300s 缓存，正常使用不会触发
- **11016 不在有效跑步时间内**：运行 `policy` 查看错误是否来自 `/api/v70103/runModePolicy`。该请求不携带界面选择的跑步开始时间；若此处被拒绝，应核对学校当前开放时段，调整记录开始时间不会改变策略请求。
- **榜单为空**：当天还没有人产生有效里程，查询会自动回退最近 3 天
- **检查更新失败**：更新仅直连 GitHub，网络不通时到 [Release](https://github.com/YanamiNeko/NekoSportsWorldTool/releases) 手动下载
- **手动更新方式**：Windows 下载新的 `nekosportsworldtool.exe` 后退出软件，用新文件替换旧文件；请保留 `config.json`、`identity.json`、`points_cache.json` 等配置文件，不要删除或修改。
- **人机验证失败**：自动重试 3 轮，仍失败大概率是网络波动，稍后再试
- **杀软误报**：未签名编译产物可能被误报，自行判断后加白

## 构建

```bash
cargo build --release                        # GUI + CLI
cargo build --release --no-default-features  # 仅 CLI
python3 -m unittest discover -s scripts -p 'test_*.py'  # 一次性与每日定时任务的离线回归
```

推 tag 自动构建 Windows / macOS / Linux 四平台产物并发布 Release（见 `.github/workflows/release.yml`）。

## 许可证

[CC BY-NC 4.0](LICENSE) © YanamiNeko（禁止商用）
