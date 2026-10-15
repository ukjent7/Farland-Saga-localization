# 补丁器（patcher）

分发给终端用户的独立可执行程序：核对游戏文件身份后备份、回放差异、原子替换，并可随时恢复原版。不依赖 Python，也不接触私有工作区。

Windows 版带图形界面，双击即用；所有平台都保留命令行与交互向导。

发布形态是**单个可执行文件**：简体中文、韩文、英文三个补丁在构建时经 `go:embed` 编进程序，用户拿到的 exe 自带全部补丁，不需要再分发压缩包，也不需要挑目录。

```bash
FarlandSagaPatcher.exe                  # 图形界面：中/韩/英/还原四个按钮
FarlandSagaPatcher.exe wizard           # 控制台向导：安装或恢复
FarlandSagaPatcher.exe install --yes    # 非交互安装
FarlandSagaPatcher.exe restore --yes    # 恢复原版
FarlandSagaPatcher.exe status           # 查看当前状态
FarlandSagaPatcher.exe verify           # 核对并返回退出码
```

## 构建

需要 Go 1.25 或更高版本。仓库不引入任何第三方模块，`go.mod` 只有标准库；图形界面直接调用 Win32 API，不需要 cgo。

```bash
cd patcher
go vet ./...
go test ./...

# 本地构建（当前平台）
go build -o farland-saga-patcher .

# 交叉编译发布目标（不含内嵌补丁）
GOOS=windows GOARCH=amd64 go build -trimpath -ldflags='-s -w -H windowsgui' -o FarlandSagaPatcher-windows-amd64.exe .
GOOS=linux   GOARCH=amd64 go build -trimpath -ldflags='-s -w' -o farland-saga-patcher-linux-amd64 .
```

上面的命令构建出的程序不带补丁，界面上的语言按钮会显示「（未提供）」。要得到面向用户的单文件版本，用统一入口的 `build-patcher`：

```bash
bash flow.sh build-patcher                                  # 内嵌中、韩、英三个候选
bash flow.sh build-patcher --stage zh-CN-unifont08          # 只内嵌指定候选，可重复
bash flow.sh build-patcher --output release/patcher.exe     # 自定义输出位置
```

该命令把 `work/release/farland-patch-<候选>.zip` 复制进 `patcher/payload/`（缺失的候选会先自动打包），再执行交叉编译。产物约 75 MB，其中补丁数据约 71 MB。`patcher/payload/*.zip` 属于私有工作区产物，已被 `.gitignore` 忽略，不会进公开仓库。

Windows 产物加 `-H windowsgui`：声明为 GUI 子系统，双击运行不会弹出控制台窗口；从终端调用子命令时标准输出仍然可用。`.github/workflows/release.yml` 在推送 tag 或手动确认时执行同样的步骤，并把可执行文件与 `SHA256SUMS` 上传为 Release 资产。补丁数据已编进程序，CI 不需要接触私有工作区。

## 图形界面

Windows 版不带参数运行（或 `FarlandSagaPatcher.exe gui`）打开主窗口。界面刻意做得很小，只有四个按钮：

| 区域 | 作用 |
| --- | --- |
| 游戏目录 | 显示程序所在目录，即推断出的游戏根目录 |
| 状态行 | 未检测到游戏目录 / 尚未安装 / 已安装某语言，忙碌时显示当前进度 |
| 简体中文、韩文、English | 各装一个内嵌补丁；已安装的语言按钮上会标注 |
| 还原原版 | 用原版备份把游戏文件恢复回去 |
| 进度条与日志 | 写入进度与全程操作记录，可选中复制 |

使用方式是把程序放进游戏根目录（与 `FarlandSaga.exe` 同级）双击运行。游戏目录由程序所在目录推断，不放对地方时按钮全部禁用，并提示缺少哪些文件。换语言直接点另一个按钮，程序会自动处理好切换，差异始终相对原版备份回放，不会叠在另一版补丁上。

安装与还原在工作线程执行，界面不会卡住；进度每 100 毫秒合并刷新一次，避免每个 4 KiB 差异块都打扰消息循环。忙碌状态用递增令牌管理：每次操作领取一个令牌，结束时只归还自己的令牌，被后续操作作废的结果也会归还，因此界面不会因为一次丢弃的检查而永远停在忙碌状态。写入过程中关闭窗口会被拦下并提示等待，另有 3 分钟看门狗兜底解除异常忙碌。

界面事件（工作线程回传的结果、启动时的补丁加载）都经一个缓冲 channel 投递，由 50 毫秒的 `WM_TIMER` 在界面线程执行。定时器必须在 `CreateWindowEx` 返回之后设置：在 `WM_CREATE` 处理期间（即 `CreateWindowEx` 内部）设置时，Windows 只投递一次 `WM_TIMER` 就再也不响，事件队列随之停摆，安装完成后界面不再更新、按钮永久禁用，看起来就是整个程序卡死。

排查界面是否真的卡住，可以设环境变量 `FARLAND_PATCHER_TRACE` 后运行：程序会把启动关键步骤与限流后的消息类型写进临时目录的 `farland-patcher-trace.log`，正常使用不写任何文件。注意 `Get-Process.Responding` 与 `SendMessageTimeout` 对由 WSL interop 启动的进程判定不可靠，会稳定报告"无响应"，要以跟踪日志与消息流为准。

界面只依赖标准库与 `user32` / `comctl32` / `gdi32` / `kernel32`，结构体布局按 Win32 SDK 逐字段核对，文件底部有编译期大小断言。非 Windows 平台没有图形界面，`gui` 子命令会给出明确提示并退回到向导。

## 命令行

| 命令 | 作用 |
| --- | --- |
| `install` | 校验身份、备份、写入补丁文件、写安装记录；失败自动回滚 |
| `restore` | 用原版备份覆盖已打补丁的文件，当前文件先存入回滚目录 |
| `status` | 展示每个目标文件的状态与安装记录 |
| `verify` | 严格核对并返回退出码，适合脚本调用 |
| `gui` | 打开图形界面（仅 Windows） |
| `wizard` | 控制台向导，任何平台可用 |

公共选项：`--game <目录>`（也可用环境变量 `FARLAND_SAGA_GAME`）、`--payload <文件>`（默认取程序所在目录的 `farland-patch-*.zip`；内嵌版可指定外部补丁包）、`--yes`（跳过交互确认）、`--json`（输出 JSON）、`-h`。

命令行面向脚本与排查：可以指定任意外部补丁包与游戏目录，不受界面「程序所在目录即游戏目录」的限制。

## 补丁载荷格式

载荷是一个 ZIP，由 `tools/package_release.py` 写入，与 `version_storage` 的差异对象格式一致：

```
manifest.json            清单：版本、语言、每个目标文件的原版/补丁身份
objects/<sha256>.gz      差异对象，文件名是补丁后文件的 SHA-256
```

`manifest.json`：

```json
{
  "format": 1,
  "kind": "farland-saga-patch",
  "game": "FARLAND SAGA I Saturn Tribute",
  "version": "zh-CN-unifont08",
  "language": "zh-CN",
  "files": [
    {
      "path": "FarlandSaga.bin",
      "original_sha256": "…64 位小写十六进制…",
      "original_size": 301359116,
      "patched_sha256": "…",
      "patched_size": 301359116,
      "object": "objects/<patched_sha256>.gz"
    }
  ]
}
```

差异对象是 gzip 流：第一行是 JSON 头（`format`、`sha256`、`size`、可选的 `base`），随后是连续的 12 字节记录（小端 `uint64` 偏移 + `uint32` 长度）与对应的数据块。回放时按记录顺序供给变更块，空隙从原版文件读取，重建结果的长度与 SHA-256 必须与对象名一致。对象头里的 `base.path` 是私有工作区路径，仅作记录；实际基准由 manifest 的原版哈希指定。

路径安全：manifest 与安装记录中的路径只接受 POSIX 相对路径，拒绝绝对路径、盘符、反斜杠与上级跳转；ZIP 成员的 CRC32 由 `archive/zip` 读取时逐块核对。

## 安装记录与备份

安装成功后游戏目录下会多出：

| 名称 | 内容 |
| --- | --- |
| `FarlandSagaPatcher-install.json` | 安装记录：版本、语言、文件清单、备份目录、状态 |
| `FarlandSagaPatcher-backup-<时间戳>/` | 原版文件（首次安装时建立，后续安装沿用） |
| `FarlandSagaPatcher-backup-<时间戳>-N/` | 上一版补丁安装前的文件状态 |
| `FarlandSagaPatcher-restore-rollback-<时间戳>/` | 恢复前保存的文件，确认正常后可删 |

安装记录中的目录名都是相对游戏目录的单段名称，因此整个游戏目录搬家后依然可以恢复。

## 安全边界

- 只修改 manifest 列出的文件；文件既不是原版、也不是本程序安装的补丁时拒绝写入。
- 差异对象始终相对**原版**回放，基准取原版备份而非游戏目录里的当前文件，因此切换语言不会把差异叠在另一版补丁上。
- 补丁文件先写临时文件并校验哈希，再原子改名；任何一步失败都从刚建立的备份回滚已替换的文件。
- 写入前会尝试以可写方式打开目标文件，Windows 上运行中的游戏会锁住镜像文件，此时明确提示先退出游戏。
- 恢复时若文件在安装后又被修改过，拒绝覆盖并提示先自行备份。

已知限制：Linux 下没有强制文件锁，进程占用检查只在 Windows 上有效；控制台输出在 Windows 上会切到 UTF-8 代码页，终端字体不含中文字形时中文可能显示为方框（不影响功能）。

## 测试

`patcher_test.go` 覆盖差异回放的往返与各类损坏拒绝（记录乱序、越界、截断、哈希不符、基准不符、头部非法）、manifest 与安装记录的路径校验、安装/恢复/切换/幂等/回滚，以及备份目录被篡改时的拒绝。测试用 Go 侧按同一格式写对象，不依赖私有工作区。

内嵌补丁的加载放在平台中立的 `embedded.go`，因此 Linux 上也能实测：`TestLoadEmbedded` 校验三个语言包都能解析、顺序固定为中韩英、每个包都声明全部目标文件且哈希合法；`payload/` 为空（刚克隆仓库还没打包）时该测试跳过。界面共用的判断（`blockedFiles`、`summarizeStates`、`samePath`）与入口分发也有单元测试。Win32 界面代码本身只在 Windows 上编译，无法在 Linux 的 `go test` 中执行，其正确性靠 `GOOS=windows go vet`、结构体编译期大小断言，以及在 Windows 上对真实载荷的安装/恢复往返来保证。
