# 补丁器（patcher）

分发给终端用户的独立可执行程序：读取 `tools/package_release.py` 生成的补丁载荷，核对游戏文件身份后备份、回放差异、原子替换，并可随时恢复原版。不依赖 Python，也不接触私有工作区，游戏数据只来自随程序分发的载荷。

Windows 版带图形界面，双击即用；所有平台都保留命令行与交互向导。

```bash
FarlandSagaPatcher.exe                  # 图形界面（Windows 版默认）
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

# 交叉编译发布目标
GOOS=windows GOARCH=amd64 go build -trimpath -ldflags='-s -w -H windowsgui' -o FarlandSagaPatcher-windows-amd64.exe .
GOOS=linux   GOARCH=amd64 go build -trimpath -ldflags='-s -w' -o farland-saga-patcher-linux-amd64 .
```

Windows 产物加 `-H windowsgui`：声明为 GUI 子系统，双击运行不会弹出控制台窗口；从终端调用子命令时标准输出仍然可用。`.github/workflows/release.yml` 在推送 tag 或手动确认时执行同样的步骤，并把可执行文件与 `SHA256SUMS` 上传为 Release 资产。补丁载荷含游戏数据，由维护者在私有工作区生成后上传到同一个 Release，CI 不接触它。

## 图形界面

Windows 版不带参数运行（或 `FarlandSagaPatcher.exe gui`）打开主窗口：

| 区域 | 作用 |
| --- | --- |
| 游戏目录 | 首次选择后记住，下次自动填上；也可直接拖游戏目录到窗口 |
| 补丁文件 | 默认取程序所在目录的 `farland-patch-*.zip`，可浏览指定或拖入 |
| 语言/版本 | 下拉列出发现到的全部载荷，换一个再点「安装补丁」即可切换语言 |
| 状态行 | 原版 / 已安装某版本 / 状态不一致，忙碌时显示当前进度 |
| 文件清单 | 每个目标文件的当前状态与大小 |
| 按钮 | 安装补丁、恢复原版、校验、使用说明，右侧是写入进度条 |
| 日志 | 全程操作记录，可选中复制 |

安装与恢复在工作线程执行，界面不会卡住；进度每 100 毫秒合并刷新一次，避免每个 4 KiB 差异块都打扰消息循环。写入过程中关闭窗口会被拦下并提示等待，防止临时文件残留。

界面只依赖标准库与 `user32` / `comctl32` / `comdlg32` / `shell32` / `gdi32` / `ole32` / `kernel32`，结构体布局按 Win32 SDK 逐字段核对，文件底部有编译期大小断言。非 Windows 平台没有图形界面，`gui` 子命令会给出明确提示并退回到向导。

## 命令行

| 命令 | 作用 |
| --- | --- |
| `install` | 校验身份、备份、写入补丁文件、写安装记录；失败自动回滚 |
| `restore` | 用原版备份覆盖已打补丁的文件，当前文件先存入回滚目录 |
| `status` | 展示每个目标文件的状态与安装记录 |
| `verify` | 严格核对并返回退出码，适合脚本调用 |
| `gui` | 打开图形界面（仅 Windows） |
| `wizard` | 控制台向导，任何平台可用 |

公共选项：`--game <目录>`（也可用环境变量 `FARLAND_SAGA_GAME`）、`--payload <文件>`（默认取程序所在目录的 `farland-patch-*.zip`）、`--yes`（跳过交互确认）、`--json`（输出 JSON）、`-h`。

控制台向导定位游戏目录（首次输入后记住），展示文件清单，已安装时直接提供「恢复原版 / 切换到其他语言 / 退出」，未安装时确认后写入。目录同时存在多个载荷时列出语言与版本供选择。

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

界面共用的判断（`blockedFiles`、`summarizeStates`、`samePath`）与入口分发也有单元测试。Win32 界面代码本身只在 Windows 上编译，无法在 Linux 的 `go test` 中执行，其正确性靠 `GOOS=windows go vet`、结构体编译期大小断言，以及在 Windows 上对真实载荷的安装/恢复往返来保证。
