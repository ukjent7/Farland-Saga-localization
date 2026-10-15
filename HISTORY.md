# 历史版本与格式记录

本页随公开仓库分发。文中的历史链接（如 `assets/previews/...`、`analysis/...`、`staging/...`、`maintenance/baseline/...`）指向私有工作区中的路径，在公开仓库里不会解析。

本页保存整理前的版本进度、格式调查和旧命令，文字中的“当前”对应记录当时。历史命令块按记录保留，不作为当前操作指引；当前只使用 WSL/Linux 的 Bash 与 POSIX 路径。日常操作从 [工作区首页](README.md) 和 [工具流程](tools/README.md) 进入；实际安装状态读取 `install_report.json`，当前候选读取 `config/storage_policy.json`。

## 补丁器界面改两屏，并修掉界面彻底卡死（2026-10-15）

上一版把界面压成「简体中文、韩文、English、还原原版」四个按钮，实际用起来文字仍然偏多。本次按使用场景重排成两屏，并修掉一个会让界面彻底卡死的线程缺陷。

界面改两屏：

- 第一屏只放标题和三个语言按钮（简体中文、韩文、English），已安装的语言按钮上带「（已安装）」标注；检测不到游戏目录时多一行提示，语言按钮全部禁用。
- 点语言按钮进入第二屏：标题变成所选语言，一行状态（尚未安装 / 已安装某语言 / 忙碌进度），**安装**与**还原**两个按钮并排，下方一个**返回**。进度条只在写入时出现，空闲时收起来，所以第二屏常态下就是两个按钮加一个返回。
- 原先的欢迎日志、全程操作记录、说明文字全部移除，`logEdit` / `appendLog` 及相关控件一并删除。
- 显隐与位置统一由 `layout()` 按当前屏与状态推导，`applyState()` 结尾再调一次 `layout()`。少了这一步，启动时按「还没找到游戏」排下的提示文字会一直挂在前屏，第二屏的按钮也会停在隐藏状态——实测时两个屏的按钮都点不到就是这个原因。

修掉界面彻底卡死：

- 现象是偶发的：程序能开、窗口能显示，但按钮全是「（未提供）」且禁用，点任何地方都没反应，CPU 占用为零，不写任何日志。关掉诊断环境变量时十次里一次都不出现，开着时十次里有三四次，一度以为是诊断代码本身的问题。
- 用内置的 goroutine 栈转储抓到现场：界面线程阻塞在 `GetMessage` 里，而 `SetTimer` 返回成功、`PostMessage` 也已调用，队列里却一条消息都没有，连第一次 `WM_TIMER` 都没来。
- 根因是 Go 调度器在系统调用之间把 goroutine 换到了别的操作系统线程上。Win32 的窗口、定时器、投递的消息全挂在创建窗口那个线程的消息队列上；窗口建在线程 A，消息循环却跑在线程 B，于是 `SetTimer` 与 `PostMessage` 的消息全进 A 的队列，B 的 `GetMessage` 永远等不到东西。修复是 `runGUI` 一进来就 `runtime.LockOSThread()`，保证建窗口和跑消息循环是同一个线程。
- 事件投递同时改成不依赖 `WM_TIMER`：工作线程把闭包放进队列后额外 `PostMessage` 一条自定义消息唤醒界面线程，`GetMessage` 一定会取到；`WM_TIMER` 退成看门狗的节拍器。早先只靠 `WM_TIMER` 当唤醒源，而它是系统合成的低优先级消息，队列一忙就可能迟迟不来。
- 窗口句柄用 `atomic.Uintptr` 单独存一份供工作线程发唤醒消息，避免跨线程读写窗口句柄的数据竞争。

验证：修复版开诊断跑 30 次、关诊断跑 20 次，全部正常；合成游戏目录上完整走完两屏交互——第一屏三个语言按钮可用，第二屏只有安装/还原/返回，安装确认框与完成框文案正确，安装后六个目标文件 SHA-256 与补丁清单一致，还原后六个文件回到原版哈希。`flow.sh test --cli-only` 36 项与完整 `test` 均通过。

## 补丁器图形界面（2026-10-14）

分发给终端用户的补丁器此前只有控制台向导，双击运行看到的是黑底命令行。本次给 Windows 版补上图形界面，安装、恢复、切换语言都变成点按钮：

- 新增 `patcher/gui_windows.go`，直接调用 Win32 API（`user32` / `comctl32` / `comdlg32` / `shell32` / `gdi32` / `ole32` / `kernel32`）实现主窗口：游戏目录与补丁文件两个输入框各带「浏览…」，语言/版本下拉框列出发现到的全部载荷，列表视图展示每个目标文件的状态与大小，按钮行是安装补丁、恢复原版、校验、使用说明，右侧是写入进度条，底部是只读日志。
- 界面不引入第三方模块，也不需要 cgo，CI 里照样直接交叉编译。窗口类、`LVITEMW`、`LVCOLUMNW`、`BROWSEINFOW`、`OPENFILENAMEW`、`MSG`、`MINMAXINFO` 的布局先用 C 程序逐字段核对（`DWORD` 在 Linux 上是 8 字节，第一轮核对因此失真，改成 `unsigned int` 后重跑），Go 侧再加编译期大小断言兜底。
- 安装与恢复放到工作 goroutine，界面线程只跑消息循环；进度回调每 4 KiB 一次，节流到 100 毫秒后经定时器合并刷新，否则消息循环会被淹没。写入过程中关闭窗口会被拦下，避免临时文件残留。
- 入口改为：Windows 版不带参数运行进图形界面，`gui` 子命令强制开窗口，`wizard` / `cli` 子命令走控制台向导；窗口建不起来时自动退回向导。Windows 产物加 `-H windowsgui` 声明 GUI 子系统，双击不再弹出控制台窗口，从终端调用子命令时标准输出仍然可用。
- `blockedFiles`、`summarizeStates`、`samePath` 从界面代码提到平台中立的 `main.go`，安装前的身份预检与 `installPatch` 复用同一判断，消除重复；三者与入口分发都补了单元测试。
- 端到端复验：用原版备份构造合成游戏目录，装 `zh-CN-unifont08` 后六个文件与 Python 安装器产物逐字节一致，恢复后全部回到原版哈希；`GOOS=windows go vet` 与 `go test` 均通过，Windows 产物为 PE32+ GUI 子系统。

已知限制：图形界面只在 Windows 版提供，Linux 版仍是命令行与交互向导；Win32 界面代码无法在 Linux 的 `go test` 中执行，其正确性靠 vet、结构体断言与 Windows 上的真实载荷往返保证。

## 公开仓库引导链（2026-10-14）

公开仓库此前只能作为私有工作区的子目录使用，社区成员拿到一份 Steam 购买的游戏安装也无法复现产物。本次补齐了从安装到首个候选的完整入口，全部只读游戏目录、不启动游戏：

- `tools/bootstrap.py` 新增 `unpack` 与 `freeze` 两个操作，并统一接到 `localize.py` 的 `unpack` 与 `assets freeze` 子命令。`steam_container.py`、`disc_decode.py`、`disc_extract.py` 整理成「模块级函数 + `main(argv=None)` 薄壳」，命令行行为不变，可被导入调用。
- `unpack` 解出四个 SSF 容器、还原数据轨、按 ISO9660 解包 `extracted/steam/` 并 staging 分析用 exe；`freeze` 冻结 `Texture.tex` 容器、五个 Unity 原版文件和字节可复现的 `unity-managed.zip`。两者都带原版守卫：检测到游戏目录安装过补丁时拒绝执行。
- `tools/extract_unity_text.py`（`localize.py extract-unity`）从冻结 Unity 原版重建 `assets/sources/unity.json`，此前该文件只能靠历史分析产物。扫描规则复现了历史 67 行的字段与行序，重建结果与冻结表字节级一致；未被语料库收录的命中写入 `unity_unmatched.json` 待人工确认。
- `tools/cd_parity.py` 把 `build_disc` 对原版光盘裸镜像 `reference_original.bin` 的硬依赖改成可选：私有工作区仍优先用真实压制盘扇区，公开仓库退回仓库内的确定性已知答案向量。实测 Steam 数据轨抽样 90 个扇区全部不满足 CD parity，社区成员无法用自己的游戏数据替代。
- `build_alpha.py` 增加 `--game` 参数并修正 `original_file` 的解析顺序，`localize.py build-first` 因此可以在没有任何既有候选的情况下建出首个候选。同时修掉 `main()` 里一处既有的 `NameError`（`input_snapshots` 用了未定义的 `name`），该入口此前没有任何工具或测试调用。
- `workspace_status.py` 的 `status`/`doctor` 在全新工作区返回未初始化结构与中文提示，不再抛裸 traceback；测试套件补建 `WORK/.tmp`、`WORK/analysis`，并对依赖私有工作区的用例加 `skipUnless`。

下面第 243-253 行附近的旧 PowerShell 命令块是这段引导链的历史形态，保留作记录。

## 工作区布局反转：仓库成为主目录（2026-10-14）

此前私有工作区 `work/` 是中心，公开仓库只能作为它的 `work/公开仓库/` 子目录存在，入口脚本 `flow.sh`/`uv.sh` 留在私有工作区、靠 glob 发现仓库。本次把两者对调：

- 仓库上移到游戏安装根下的 `farland-saga-localization/`，成为主工作目录；`flow.sh` 与 `uv.sh` 移入仓库，直接运行仓库内的 `tools/localize.py`，不再有发现逻辑。
- 逆向产物继续留在仓库同级的 `work/`，内部子文件夹名一个未改，因此 `bundles.json` 的快照键、候选清单与 `install_report.json` 里的绝对路径全部原样有效，恢复链零改动。
- `tools/path_compat.py` 的 `WORK` 默认值从「仓库父目录」改为「仓库同级的 `work/`」，`FARLAND_WORK` 仍可覆盖；`WORKSPACE` 仍是游戏安装根，`ROOT = WORK.parent` 定位游戏目录的逻辑不变。
- `uv.sh` 在私有工作区工程（含 Hex-Rays IDALib 本地 wheel）存在时优先使用它，单独检出仓库时用仓库自己的 `pyproject.toml`；`.python-version` 与 `.editorconfig` 一并移入仓库。
- 私有工作区里失效的 `.gitignore` 与 `.gitattributes` 随之移除——该目录本来就不在任何 git 仓库内。

社区成员现在把仓库克隆到任意位置，产物都落在克隆目录同级的 `work/`，目录布局与维护者一致。

## 术语表对照修正与台版/大陆版规范统一（2026-10-09）

按用户要求，对 `work/translation/glossary.json`（全表 90 条）进行了系统的联网考据、校对与状态转正，深入比对 1997 年台湾 TGL（帝技爷如／智冠总经销）繁体中文版与大陆官方引进版（如成都众心电子）及现代通用规范：
1. **原则与规范**：尽量套用台版或大陆官方术语；对当年存在明显音义偏差、粗劣附会或严重失当的离谱旧译，坚决予以修正，统一采用现代规范译名，并在每条 `notes` 中详尽保留历史译名对照与修订原由。
2. **离谱旧译坚决修正**：
   - `ファム`（Fam）：台版原译“發牡”（华语游戏界最恶名昭彰的离谱译名之一，语感极差且具牲畜歧义），坚决修正为正统规范译名**“法姆”**。
   - `レオン`（Leon）：台版原译“雷龍”（当年将青年骑士主角误按音/义附会为恐龙/爬行动物），坚决修正为现代通用规范**“里昂”**。
   - `エントリヒ`（Entrich）：台版原音译为“彥都梨希”（生造汉字附会），修正为德式标准音译**“恩特里希”**。
   - `ヨーク`（York）：台版原音译为“優葛”（音如酸奶），修正为正规地名**“约克”**。
   - `バース`（Barth）：台版原音译为生造字“琶斯”，修正为正规地名**“巴斯”**。
   - `ブライアン`（Brian）：台版旧译“布萊伊”（漏译尾音），修正为规范译名**“布莱恩”**。
   - `リアン`（Rian/Lian）：台版旧译“梨安”（用字生硬怪异），修正为规范人名**“莉安”**。
   - `エレナ`（Elena）：台版旧译“繪蕾安”（音节颠倒），修正为规范译名**“艾莲娜”**。
   - `グレン`（Glen）：台版旧译“古廉”，修正为规范译名**“格伦”**。
   - `デスフォルト`（Death Fault）：台版旧译“猛打”（过于土俗简陋，丧失原技名“致命断层/空间断裂”之剑技意境），修正为贴合原设的**“次元斩”**。
3. **权威与通用译名沿用**：
   - 角色：沿用台版译名甘特（Gant）、阿维（Avi）、韦尔纳（Werner）、德拉戈（Drago）；沿用通用译名奥菲莉亚（Ophelia）、玛西亚（Marcia）、露西亚（Lucia）、卡西姆（Kasim）、诺拉（Nora）、凯撒（Kaiser）、巴尔加（Barga）、卡琳（Karin，台版FS2时空道标作卡羚）、Ｔ．Ｔ．等。
   - 地名与术语：沿用乌尔卡努斯山、巴塞尔、赫尔曼三世、赫利俄斯、守护天使（梅西亚、艾莉西亚）等；魔族领地トゥルク统一规范为特鲁克（台版作土耳克）。
   - 单位兵种与魔物：46 类战场单位与魔物规范化，将“スペクター”精准校正为“幽魂”（区别于“ゴースト”普通幽灵）。
4. **状态与完整性**：全表 90 项条目状态统一由“草稿/暂定/译稿”变更为**“正式”**，分类细化为人名（25）、地名（7）、称呼（2）、通用（4）、战斗（2）、人物／单位（46）、术语（1）、技能（1）、守护天使（2），100% 保持稳定 ID 与 JSON 结构完整性。CLI 入口测试全部通过。本轮仅记录译稿更改，不打包新版本镜像。

## 剧情与界面译稿重译与开场初稿解耦（2026-10-09）

按用户要求，完成了剧情事件主表、关卡图片标题、界面图块标签及外层子菜单译稿的独立重译与开场初稿解耦，本轮**仅记录译稿更改，不打包新版本镜像**：
1. **开场初稿解耦**：`work/tools/build_alpha.py` 解除了对 `translation/opening.zh-CN.json` 的硬编码依赖，字体预览采样与开场行数直接从 `translation/events.json` 提取（`EVE/EVE01_1.EVT`）；原初稿已归档至 `maintenance/baseline/opening-draft-20261009/`，`translation/README.md` 同步更新。
2. **关卡标题重译**：通过隔离的子代理清空了 `translation/picture_titles/titles.trilingual.json` 的原有旧译文，并在严禁翻阅任何其他文件的环境下，由独立子代理基于日文原文重译了全部 50 个关卡/章节标题的中、韩、英三语文本（`title:01` 至 `title:50`）。
3. **界面标签重译与补齐**：按同样两阶段子代理流程处理了 `translation/picture_titles/ui_labels.trilingual.json`，先清空旧译文，再由独立隔离子代理进行三语翻译。完整补齐了此前 `equipment`（装备/持有物 4 项）与 `stats`（基础属性 7 项）缺失的简体中文版本，同时重译了 `terrain`（地形 8 项）。英文属性和装备采用符合界面像素规格的标准缩写（HP, MP, ATK, DEF, INT, RES, AGI；Wpn, Armr, Gear, Item）。
4. **外层子菜单重译**：按同样两阶段子代理流程处理了 `translation/frontend_submenus.json`，清空旧译文后由独立隔离子代理重译了全部 110 个外层菜单条目（弹窗确认、快速存读、画面显示、按键映射、提示说明等）。英文版纠正了日厂拼写瑕疵（如 `Registable` -> `Registrable`）并保持了徽章折行排版。
5. **剧情事件全篇重译**：将 `translation/events.json` 全篇 1,971 条剧情台词（覆盖 37 个 EVT 文件）切分为 6 个批次，通过 6 个并发隔离子代理完成地道简体中文重译。严格遵守统一专有名词表与字符容量限制（`len(translation) <= original_units` 超长率为 0%），并通过 `bash work/flow.sh merge --table events` 安全校验合并入主表。
6. **版本状态保留**：本轮操作未运行打包命令，未生成新候选，未修改现有六个安装文件与安装记录。当前候选与安装版本仍为 `zh-CN-unifont07` 等，待后续统一构建测试。

## 工作区目录整理（2026-10-09）

旧版 Saturn 镜像和仅适用于该镜像的第三方韩语补丁已归入 `references/legacy/`，并保留原文件内容。项目补充了目录与数据生命周期说明、Python 缓存忽略规则和 Linux 端文本与二进制文件格式约定；旧 PowerShell 包装脚本集中归档，不再参与当前流程。当前译稿、候选、安装记录和恢复归档仍按既有工作流管理；详见 [项目结构图](README.md)。

## 译稿数据改用 JSON（2026-10-09）

当前简体中文可编辑源为 `translation/events.json`、`translation/catalog.json` 和 `translation/glossary.json`。JSON 行保留稳定 ID、原文、偏移、容量、字形索引及译稿状态；AI agent 输出按 ID 经 `flow merge --table ...` 合并。构建、审阅与新提取流程均读取或生成 JSON。旧版 TSV 只保留在冻结基线和历史快照中，不再作为当前输入。

## 商店价格清零原因（2026-10-03）

用户在中文 05 的商店看到阔剑价格为 0，并发现购买不扣钱。已确认为项目构建错误：`ITEM.DAT` 每记录 36 字节，名称范围为 `+0..17`，`+18..19` 是大端两字节价格；旧提取器标记名称容量为 20，名称写入时将价格一起填零。中文 05 和三语 06 都有 173 条原本非零的价格变为零；`MAIN.DAT` 的 `0x2A5E4` 内嵌副本同步带入错误。其余 `+20..35` 属性未改变。阔剑、长剑、短剑原价分别为 300、150、60。

IDALib 静态证据：`0x0604C830` 从记录 `+18` 读取两字节价格，地址为 `0x06089740 + 0x1F40A + 36*道具索引`。核对详情见 [原因报告](analysis/shop_price_root_cause.json)。补丁配置 `item-prices` 恢复两处原价，公共文字构建器限制名称写入范围并检查全部属性，八条超长英语名称在配置中保留原词和缩写。既有译稿、冻结输入、ID、偏移和复核记录未覆盖。修复测试与候选只写入 `work/`，未启动或安装游戏。

三语 07 已在 06 基础上打包完成，保留全部存档标题写入；六文件哈希与最终镜像加密回读通过。`test --pack` 的 13 项入口和 16 项工作流检查通过，另补充中文原稿构建检查通过。原价、全部属性、运行时副本及 24 处存档标题立即数核对见 [完成报告](analysis/shop_price_completion.json)。该记录形成时（2026-10-03）仍安装中文 05。脚本基线见 `maintenance/baseline/shop-prices-20261003/`。

## 存档文件夹标题三语修正（2026-10-03）

用户在中文 05 中发现存档窗口标题“フォルダ 1”未翻译。原版游戏的标题记录实际是 `1ST.BIN` 的“本体 RAM／卡带 RAM”，现有译稿已经翻译；日文来自模拟器的运行时覆盖。IDALib 静态分析定位到 `FarlandSaga.exe` 的 `0x140040D30`、`0x140040EC0` 和 `0x140041060` 三个函数，每处将 `0x0601B828` 与 `0x0601B83C` 开始的四个字格写入“フォルダ”，并保留原编号 2／1。字符串拆为逐字写入指令，因此普通文本扫描无法找到整词。

新增累计补丁配置 `save-folder`，通过通用 `runtime_word_writes` 处理器改写 24 个文字立即数。中、韩、英分别使用“文件夹／폴더／Folder”，复用已有字形映射并补齐原空白字格。冻结原可执行文件哈希、完整指令字节、内存写入地址和调用目标；只有声明的文字立即数可改变，操作码、分支、编号、前后空白与存读档逻辑不变。后续字库同步保留当前可执行文件的其他补丁，完整韩英重建校验字位并回放运行时文字，支持仅资源候选作为基础。

生成 `zh-CN-unifont06`、`ko-KR-unifont-full06` 和 `en-US-unifont-full06`，均为六文件成品。仅 `FarlandSaga.exe` 变化，原生资源、游戏镜像、Unity 文件及已翻译的外层图集与对应 05 逐项一致；没有新增字形或更改存档。预览见 [存档标题三语预览](assets/previews/save-folder-trilingual-848efed051962ee7.png)，核对记录见 `analysis/save-folder/completion.json`，静态反汇编与排查材料保存在同目录。

`test` 的 13 项入口检查和 14 项工作流检查通过，覆盖六处改写、三语字形回读、其他字节保护、重复应用、完整重建、后续字体更新，以及错误原版身份、错误指令与容量溢出拒绝。日志为 `analysis/save-folder/tests-passed.log`。新候选尚未安装，实际仍安装中文 05；未启动游戏。变更前脚本快照为 `maintenance/baseline/save-folder-20261002-210836/`。

## 中文 05 安装供测试（2026-10-03）

按用户要求安装最新 `zh-CN-unifont05`。`install --check-only` 六文件预检通过后安装，原英语 full02 已保存为切换前备份。安装包含中文游戏文字及最新外层菜单、子选项、确认弹窗和 View 提示；安装记录见 [install_report.json](install_report.json)，日志见 `analysis/frontend-submenus/install-zh05.log`。未启动游戏，用户使用普通存档或新游戏测试，避免旧快速状态携带原字库与文字。

## 外层子菜单三语候选（2026-10-03）

在 04 的主菜单与 View 面板基础上，`frontend-submenus` 累计配置补齐快速存读档、画面／窗口设置、按键动作、额外选项、语言选项、退出／切换确认、操作说明、在线排名重置警告、加载文字与无存档占位图。新增译稿保存在 `translation/frontend_submenus.json`，110 组固定 ID 对应全部原语言槽；累计修改 371 个文字块及 7 个 View 面板。来源核对联系表、配置编译记录与完成报告保存在 `analysis/frontend-submenus/`。

公共图集处理器支持显式多行、局部文字区域、背景色、文字颜色、描边、对齐与 90 度旋转，用同一份声明配置处理加载文字、徽章和横竖无存档提示。字体字符覆盖与实际墨迹容量检查通过，禁止截断、空行、未声明换行和其他控制字符。加载图标、徽章边框、499 个 sprite 坐标、Parts/Anime 元数据、原加密头及文字区以外像素保持一致；加密图集回读逐像素核对通过。

生成 `zh-CN-unifont05`、`ko-KR-unifont-full05` 和 `en-US-unifont-full05`，共六个安装文件；除 `Texture.tex` 外五个成品文件与各自 04 基础逐项哈希一致。旧八个菜单及七个 View 面板逐像素一致。分组图像、三语实物预览与可搜索译稿见 [三语审阅页](analysis/frontend-submenus/review.html)。操作指南正文、独立按键指南图片和制作人员页面属于其他资源，未列入此次子选项翻译。

`test --pack` 的 13 项入口／安装检查和 12 项工作流检查全部通过，包括新子菜单的三语回读、覆盖范围、旧菜单保留、加载图标与其他图块保护，以及原生最终加密镜像重放。日志见 `analysis/frontend-submenus/test-pack.log`。新候选尚未安装，实际安装仍为英语 full02；未启动游戏，实际显示待用户测试。变更前脚本快照保存在 `maintenance/baseline/frontend-submenus-20261002-202007/`。

## 外层菜单三语候选（2026-10-03）

用户在英语 full02 中发现外层菜单下方仍显示日文“ビュー ボタン”。原版 `Texture.tex` 为 SSF 加密的 Texture/Parts/Anime 图集，解密后 PNG 为 2370×4394；Parts 固定 499 个 sprite。八个菜单标题引用同一套英文 sprite（404、399、382、426、360、398、375、386），默认 View 提示引用日文 sprite 108，导致语言混用。静态依据保存在 `analysis/frontend-localization/`，使用 `/home/kk` 的 IDALib，未启动游戏。

`frontend-menu` 配置生成简体中文 `zh-CN-unifont04`、韩语 `ko-KR-unifont-full04`、英语 `en-US-unifont-full04`，覆盖上述八个标题及提示面板 104–110。菜单文字与“选项／옵션／Options”“View 键／View 버튼／View button”分别匹配候选语言；全部原语言槽使用同一候选提示。文字宽高及字体字符覆盖均通过检查，字体为 Windows SimHei、Malgun Gothic、Arial。原版冻结于 `translation/frontend_baseline/Texture.tex`，候选保存原版输入、配置、字体哈希和回读报告。

候选增加第六个安装文件 `Texture.tex`，原有五个成品文件和原生资源与基础候选逐项一致。图集尺寸、499 个 sprite 坐标与 Parts/Anime 元数据、原加密头及文字区以外像素保持一致，最终加密图集解密回读通过。通用候选构建器支持登记新增外层文件；完整韩英译稿重建保留这些图集补丁。安装器已有的动态清单会纳入新文件的预检、原版备份和回滚，切回旧版时恢复原图集。

三语预览见 [菜单预览](assets/previews/frontend-menu-trilingual-efeb5f5604771789.png)，核对记录见 [完成报告](analysis/frontend-localization/completion.json)。`test --pack` 的 13 项入口／安装安全检查及 11 项工作流检查全部通过，包括新图集的三语回读、文字溢出与原版身份拒绝、旧图块保护、完整译稿重建保留新图集，以及已有完整加密镜像重放。三份新候选尚未安装；本次此前按用户要求安装的是英语 full02，真实游戏显示待用户测试。

## 旧状态（2026-10-03 较早记录）

**韩语 Unifont full03 已安装，英语 full02 仍为未安装候选。** 按用户要求，中韩菜单“情报／道具／装备”中间增加一个空字格，与上面选项同为48像素宽；英语未改动。中文Unifont03候选已生成。见 [中韩实际字距预览](assets/previews/menu_spacing_zh_ko-895f426f7022c3c4.png) 和 [核对记录](analysis/condition_labels/menu_spacing_completion.json)。 新增修正队伍列表“正常”及其余八项状态文字，共 2,786 条；此前固定四字节、无终止符的状态表漏提取，现已补齐，韩语“正常”为“정상”。详见 [状态修正](translation/languages_full/condition_review.html) 和 [定位记录](analysis/condition_labels/README.md)。 按本轮要求，GPT-6 Luna 补齐两版各 2,777 条已识别文字，对齐中文 alpha06，并交叉复核剧情与说明。补齐装备窗图片标签及“属性”图块，保留 50 个关卡图片标题、30 份地形表和既有属性标签。图片通过原图块修改处理，没有使用生图。最新成品在 `staging/ko-KR-unifont-full03/` 与 `staging/en-US-unifont-full02/`；详见 [完整候选说明](translation/languages_full/README.md)、[四语审阅页](translation/languages_full/review.html) 和 [最终核对报告](translation/languages_full/completion_report.json)。

两版均检查文字容量、字形、资源副本、Steam 独立说明字库及外层界面字体，最终加密镜像修改扇区回读通过。未启动游戏；实际显示与全流程待后续测试。韩语已替换五个游戏文件，安装后哈希与候选一致，原韩语 full02、full01、test04 和原版均有完整备份。安装记录见 [install_report.json](install_report.json)，本轮安装核对见 [installation_status.json](translation/languages_full/installation_status.json)。请重新启动游戏，使用游戏内普通存档或新游戏测试；模拟器快速状态可能保留旧文本和字库。

韩语 test04 为此前安装并已备份的版本，含 RESET.DAT 属性图块漏改修正，但普通文字仍是 378 条短测试译稿；旧报告见 [属性修正说明](staging/ui-labels-previews/README.md)。中文候选现为 `staging/zh-CN-unifont03/`。此前 Unifont 字库及关卡标题说明见 [历史说明](staging/unifont-previews/README.md)。

**alpha06-dashfix 为更早生成的独立破折号候选，未单独安装；后续 Unifont 版本已包含横线居中的处理。** 该候选将破折号从字格第 0 行下移至第 8 行，并同步 Steam 独立说明字库。其余字形、译文、游戏资源及 Unity 文件与 alpha06 一致，镜像回读核对通过。见 [修正记录](staging/alpha06-dashfix/CHANGELOG.md) 和 [实际字库对照](assets/previews/dash_preview-2519269c16825f82.png)。

**中文 alpha06 为本次 Unifont 更新的基础版本，安装前已备份。** 按用户要求补齐剩余非图片游戏文字，新增翻译全部交由 **GPT-6 Luna** 完成，并进行了交叉复核。覆盖 2,777 个条目：1,971 条剧情、193 个道具／装备名称、65 个单位名称、65 个技能名称、115 条菜单／界面提示、173 条道具／装备说明、62 条人物／敌人介绍、44 条技能说明、22 条 Steam 控制器名称及 67 条 Steam 外层文字。

- 所有已识别说明表的非空记录均已汉化；原本为空或不存在的说明保留原样。
- 修正 UNIT1.DAT 的旧提取间隔，补齐敌人名称，并同步 MAIN.DAT 的运行时副本。
- 固定补字位共 574 个，保留旧版 506 个字位与字形；Steam 独立说明字库同步。
- Steam 外层添加 SimHei 动态字体回退，覆盖新增简体字；图片、纹理、视频、语音、音乐及存档未改动。
- 完成文字容量、原指令和资源副本核对；700 个修改扇区从最终加密镜像解密回读一致。未启动游戏，实际显示和全流程由用户通关测试。
- 安装文件包括 `FarlandSaga.bin`、`FarlandSaga.exe`、`S-Tribute_Data/resources.assets`、`S-Tribute_Data/sharedassets0.assets`、`S-Tribute_Data/level0`。安装前版本已完整备份，恢复脚本覆盖这 5 个文件。

详情见 [alpha06 修改记录](staging/alpha06/CHANGELOG.md)、[完成范围与核对报告](staging/alpha06/completion_report.json)、[安装记录](install_report.json) 和 [完整译稿审阅页](review.html)。译文及复核记录在 `translation/alpha06/`。请使用新游戏或游戏内普通存档进行测试，模拟器快速状态可能携带旧字库和旧文本。

## 历史记录（以下状态仅对应当时版本）


## 当前状态

**2026-10-02 已生成韩语与英语短测试版，各 378 条，尚未安装。** 范围为开场 50 条、菜单与名称 324 条、说明 4 条；GPT-6 Luna 根据原版日文独立翻译并处理交叉复核修正，未使用第三方韩语补丁。成品分别在 `staging/ko-KR-test01/` 与 `staging/en-US-test01/`，切换与恢复方法见 [韩英测试说明](storage/README.md)。本轮未启动游戏，当前安装仍为中文 alpha04。

**alpha05 候选成品已于 2026-10-02 生成，尚未安装。** 已提取的 1,971 条剧情及 324 条菜单／名称均有中文初稿，加上原有 4 条说明，共 2,299 条。新增 1,921 条剧情及 65 个技能名称由 **GPT-6 Luna** 翻译并复核；主代理处理容量、字符编码、资源副本与封装。成品、报告和冻结的输入均在 [staging/alpha05/](staging/alpha05/)，详情见 [alpha05 修改记录](staging/alpha05/CHANGELOG.md)。

- 所有 37 个有文本的事件文件已回填；另有 19 个事件文件没有导出的文本，未改动。
- 6 个事件文件精简未使用的字符表项；全部仍在原 ISO 分配空间内，事件入口、脚本长度与非文本指令字节保持一致。
- 固定补字槽位共 506 个，含 alpha04 的 165 个旧槽位和本次新增 341 个；Steam 独立说明字库同步更新。
- 700 个修改扇区已从最终加密成品解密回读，未涉及的外层块保持一致；固定新字位后重建的 44 个资源与成品一致。
- 新版未在游戏内验证；少数命令台词与技能专名已在译表备注中标记待确认。其余道具说明及图片文字仍待处理。
- [译稿审阅页](review.html) 已更新，所有新增翻译及复核记录在 [translation/luna_alpha05/](translation/luna_alpha05/)。工作过程未启动游戏，根目录仍为已安装的 alpha04。

**alpha04 已于 2026-09-30 按用户要求安装，安装文件哈希核对通过**，修复后的游戏内显示待用户复查。消耗品说明的异形字已查明：Steam 移植版额外绘制说明，读取 EXE 内嵌的独立字形表，而非镜像中的 `KANJI.FON`。alpha04 同步该字形表，保留 alpha02 准确译文与剧情推进修正；镜像与 alpha02 完全相同。详情见 [排查记录](analysis/item_description_root_cause.md) 和 [alpha04 修改记录](staging/alpha04/CHANGELOG.md)。代理未启动或操作游戏。

- 已替换游戏根目录中的 `FarlandSaga.bin` 和 `FarlandSaga.exe`，安装后的 SHA-256 已与成品一致。
- 原版完整备份：`backups/alpha01-20260930-104848/`；首版备份：`backups/alpha02-prior-20260930-111716/`；alpha02 备份：`backups/alpha03-prior-20260930-114634/`；回退前 alpha03 备份：`backups/alpha02-prior-20260930-123827/`。安装记录见 [install_report.json](install_report.json)，候选成品报告见 [staging/alpha04/build_report.json](staging/alpha04/build_report.json)。
- 已安装的 alpha04 覆盖开场 50 条对话、32 条菜单、193 个非空道具／装备名称、34 个单位名称及 4 条道具说明，共 313 条。
- 首版漏改 `MAIN.DAT` 内嵌表的问题已修正；句尾 211 个补齐空格改为文本结束符之外的空语句分隔符。具体原因和静态依据见 [alpha02 修改记录](staging/alpha02/CHANGELOG.md)。
- 原字库补入 165 个字形，alpha03 保留 alpha02 的补字槽位和位图；实际字库预览见 [staging/alpha03/font_preview.png](assets/previews/font_preview-405b392a2eb54612.png)。
- 重新启动后建议通过游戏内存档继续，或选择新游戏；模拟器的快速状态可能保留更新前的内存资源。
- 安装未改动存档；测试中的错字、缺字、文本溢出、卡住或崩溃，请记录截图与出现位置。

### 恢复原版

先退出游戏，在游戏根目录的 PowerShell 中运行：

```powershell
pwsh.exe -NoProfile -ExecutionPolicy Bypass -File .\work\restore_original.ps1
```

恢复脚本会核对备份和当前文件哈希，再恢复上述两个文件；不会改动存档。Steam 更新造成文件改变时，脚本会停止，避免覆盖更新后的内容。

### 资源与译稿进度

- Steam 数据轨道已解出，ISO 目录包含 1,410 条文件记录。其中音轨文件 `SEGA_WRN.DA` 指向数据轨以外，未从数据轨提取。
- 56 个事件文件已导出：1,971 条文本、39,333 个原文字符单位，未出现无法识别的字符索引。
- 1,971 条剧情均有简体中文初稿；其中开场 50 条沿用旧稿，其余 1,921 条由 GPT-6 Luna 完成。
- 菜单及名称表 324 条均有初稿：32 条菜单、193 个道具名称、65 个技能名称、34 个单位名称。65 个技能名由 GPT-6 Luna 补齐。
- 剧情与名称表合计 2,295 条中文初稿，另有 4 条道具说明，共 2,299 条。人名、地名和专有名词暂定，尚未全部在游戏内校对。
- 工作文件、静态分析数据库及输出均在本目录。alpha04 已按此前指令安装，alpha05 为待安装候选成品；未启动游戏。反汇编使用 `E:\IDA` 的 IDALib。

## 审阅与继续翻译

- [译稿审阅页](review.html)：可按中文初稿、待翻译、文件或文字搜索。
- [剧情译稿](translation/events.json)：保留稳定 ID、原始偏移、字符索引和译文状态。
- [菜单与名称译稿](translation/catalog.json)：包含原始偏移及字段容量。
- [开场初稿](translation/opening.zh-CN.json)：开场事件的 50 条译文。
- [术语表](translation/glossary.json)：里昂、拉尔夫、巴斯及 Luna 补充的专名暂定译法。

每条 JSON 的 `translation` 为译文，`status` 使用 `untranslated` / `draft`。修改译稿时保留 `id`、`offset` 和 `raw_codes`。JSON 是工作区译稿格式；游戏文件仍须经构建器编码为原生字形及字符索引。

## 资源与工具

| 路径 | 用途 |
| --- | --- |
| `decoded/FarlandSaga.cue` | Steam CUE 明文 |
| `decoded/FarlandSaga_Steam.ini` | Steam 配置明文 |
| `decoded/FarlandSaga.bin` | 通用封装解出后的镜像内层，仍含扇区加密与排列 |
| `decoded/FarlandSaga_track1.bin` | 还原顺序并解密后的 Steam 数据轨道 |
| `extracted/steam/` | 本项目使用的 Steam 资源 |
| `extracted/original/` | 初版土星参考资源 |
| `extracted/korean/` | 韩语补丁参考资源 |
| `analysis/` | IDALib 数据库、反编译结果、资源差异及回封装报告 |
| `staging/roundtrip/` | CUE 和配置文件的原样回封装结果，供比对 |
| `staging/alpha/` | 首次可测试补丁、字形映射、资源及构建核对报告 |
| `staging/alpha02/` | 道具内嵌表与对话句尾等待修正版本 |
| `staging/alpha03/` | 两条道具说明的字形兼容修正版，保留 alpha02 对白和其他资源 |
| `staging/alpha04/` | 当前已安装版本，同步 Steam 独立说明字库 |
| `staging/alpha05/` | 全部已提取剧情与名称的候选成品、输入快照及报告 |
| `translation/luna_alpha05/` | GPT-6 Luna 分批译稿、复核记录与本轮开始时的备份 |
| `backups/` | 安装前的原版程序与镜像 |
| `tools/` | Python 解包、文字导出、译稿合并和审阅页生成工具 |

工具依赖统一由 uv 锁定；IDALib 使用 `/home/kk/ida-pro-9.4` 的本地库和现有激活配置，详见 [Python 环境说明](tools/README.md)。韩语补丁的旧版 xdelta 不支持这些 Unicode 路径，参考补丁复制为 `reference_korean.xdelta` 后再处理。

## 已确认的格式

### Steam 通用文件封装

`steam_container.py` 从当前 `FarlandSaga.exe` 读取表数据，并按原文件名验证头部。头部为 2,048 字节；正文分为 2,048 字节加密单元，缓存块为 32,768 字节。末尾不足一个单元时采用单独的字节处理方式。

静态依据为 `MPS::File::CDecryptFile`：`0x1407B1CC0` 计算文件名参数、`0x1407B1880` 读取并识别头部、`0x1407B22C0` 解密正文；密钥初始化在 `0x140070D90`。

原 CUE 与配置文件完成解密、再编码后，均与安装文件逐字节一致。报告见 `analysis/roundtrip_report.json`。这确认了通用层，不等同于修改镜像后可正常运行。

### Steam 镜像专用层

`disc_decode.py` 实现镜像扇区排列和扇区解密。每扇区 2,352 字节，每组 64 扇区；本镜像实际使用排列表编号 **1**。数据轨有 128,075 个扇区，解出大小 301,232,400 字节。

静态依据为 `0x1400E97F0`（表初始化）、`0x1400E9890`（密钥处理）、`0x14070E7B0`（排列读取）、`0x1400E9900`（扇区解密和校验）。排列差值表存放在 `0x140D2D2D0`，压缩解码依据 `0x1400DFA30`。

`0x1400E9900` 对解出的扇区计算校验值，并在不匹配时二次解密，破坏已修改的扇区。alpha01 将 `0x1400E9BD1` 的 `74 32` 改为 `EB 32`，跳过这段二次解密；程序仅修改一个字节。当前仍计算校验值，但不再执行该不匹配处理。未来可研究更新原校验表以替代分支修改。

本版修改 374 个逻辑扇区，重新计算 MODE1 EDC/ECC、执行扇区加密与排列，再回封装 Steam 镜像。最终加密成品的所有修改扇区均重新解密比对；未涉及的外层加密块逐字节保持原样。EDC/ECC 算法核对参考为 [ECM 源码](https://github.com/alucryd/ecm-tools/blob/master/ecm.c)，并与原始光盘扇区的校验字节一致。这些离线核对不等同于游戏内测试通过。

上段扇区数量对应首版 alpha01；alpha02 修改 522 个逻辑扇区，执行同样的回封装核对。

### 事件文字

EVT 的多字节数值采用大端序：

1. `u16` 事件入口数量；每个入口为 `u16 id` + `u32` 相对于脚本起点的偏移。
2. `u16` 字符表数量，随后为每字符两个字节的 CP932 字符表。
3. 脚本中 `0x22` 开始文本，后续 `u16` 是从 **1** 开始的字符表索引，`0x0000` 结束文本。
4. `0x25` 后为四字节整数字面量，`0x27` 后为零结尾字节串，`0x40` 后为一字节函数编号；导出时跳过这些参数，防止误识别为文字。

开场文件为 `EVE/EVE01_1.EVT`。alpha01 保留所有事件入口、脚本长度和非文本指令字节，较短译文用全角空格补齐原字符单位数；扩大字符表后，目录文件大小由 9,104 更新为 9,470 字节，仍在原分配空间内。其余部分的命令含义、换行与窗口容量仍需分析；索引解码已经完整导出可读文字。

alpha02 将结束符提前到译文实际末尾，余下字节填入 `$` 空语句分隔符，保留所有原指令位置及入口偏移。解析器对没有活动调用的 `$` 在同一循环跳过，依据为 `0x06038574–0x060385B4`。修正版不会播放这些句尾空白字符；实际手柄推进手感仍需用户确认。

### 菜单和名称

- `1ST.BIN` 的 `0x1232C–0x12487` 是一组零结尾菜单字符串。
- `SYSTEM/ITEM.DAT`：200 个 36 字节记录，名称字段为前 18 字节（含终止符）；随后 2 字节为大端价格。旧调查误记为名称 20 字节，导致旧汉化构建清零价格，2026-10-03 已定位并修正。
- `SYSTEM/MAGIC.DAT`：90 个 32 字节记录，名称字段为前 22 字节。
- `SYSTEM/UNIT1.DAT` 的前 35 个 72 字节记录包含 18 字节名称字段；后续数据未按此结构处理。
- `SYSTEM/KANJI.FON` 大小 249,792 字节，每字形为 16×16 单色位图、32 字节。CP932 高字节 `hi`、低字节 `lo` 的索引为 `(hi-(hi<0xA0 ? 0x81 : 0xC1))*188 + lo-(lo<0x7F ? 0x40 : 0x41)`。alpha01 保留可直接使用的原字形，将新增中文字形分配到未被已提取剧情及名称、主程序字节序列占用的槽位；映射见 `staging/alpha/glyph_map.json`。
- `SYSTEM/MAIN.DAT` 内嵌运行时道具表（`0x2A5E4`）、技能名称字段（表起点 `0x2C204`）、单位名称区（`0x1CEC`）和说明资源（`0x3A944`）。alpha05 同步这些副本；技能表只更新名称字段，保留与独立资源不同的属性字节。当前固定字符映射见 `translation/glyph_slots.zh-CN.json`。

### 版本差异

Steam 与初版的事件文件、字库、道具和技能资源哈希一致。共 10 个可读取资源不同：`1ST.BIN`、8 个视频文件、`SYSTEM/TITLE.DAT`。ISO 中的布局也有变化。

韩语补丁修改 133 个资源，其中 37 个 EVT 文件；它适合研究字符表和图片文字的处理方式，不能直接覆盖 Steam 镜像。差异详情见 `analysis/steam_vs_original.json` 和 `analysis/reference_changes.json`。

## 常用命令

在游戏根目录运行，下列输出都写入 `work`：

```powershell
.\work\uv.ps1 run --locked python work/tools/steam_container.py FarlandSaga.cue work/decoded/FarlandSaga.cue --name FarlandSaga.cue
.\work\uv.ps1 run --locked python work/tools/steam_container.py FarlandSaga_Steam.ini work/decoded/FarlandSaga_Steam.ini --name FarlandSaga_Steam.ini
.\work\uv.ps1 run --locked python work/tools/steam_container.py FarlandSaga.bin work/decoded/FarlandSaga.bin --name FarlandSaga.bin
.\work\uv.ps1 run --locked python work/tools/disc_decode.py work/decoded/FarlandSaga.bin work/decoded/FarlandSaga_track1.bin --file-index 1
.\work\uv.ps1 run --locked python work/tools/disc_extract.py work/decoded/FarlandSaga_track1.bin work/extracted/steam
```

以下命令仅用于空白工作区首次导出原文。提取器遇到已有 JSON 会拒绝覆盖；继续翻译时应合并 JSON 并只重建审阅页。

```powershell
.\work\uv.ps1 run --locked python work/tools/extract_text.py work/extracted/steam work/translation
.\work\uv.ps1 run --locked python work/tools/apply_drafts.py work/translation/events.json work/translation/opening.zh-CN.json
.\work\uv.ps1 run --locked python work/tools/extract_catalog.py
.\work\uv.ps1 run --locked python work/tools/build_review.py
```

IDALib 静态分析：

```powershell
.\work\uv.ps1 run --locked python work/tools/ida_inspect.py --functions 0x1400e9900
.\work\uv.ps1 run --locked python work/tools/ida_sh2.py --functions 0x06004318
```

## 下一阶段

1. 在明确要求安装 alpha05 后，正常退出游戏再安装；安装工具会备份现有 alpha04。
2. 用户在游戏内复查字形、对白推进、技能名称与窗口显示，优先关注精简字符表的 6 个事件文件和译表中标记的待确认项。
3. 继续提取并处理其他道具说明、图片文字及未纳入现有译表的资源；翻译继续交由 GPT-6 Luna。
4. 研究原扇区校验表更新方案，减少对主程序的修改。

构建命令：

```powershell
.\work\uv.ps1 run --locked python work/tools/build_alpha.py --stage alpha05 --version alpha05
```

只准备资源和容量报告时加 `--resources-only`；完整构建会回封装镜像并完成离线核对。事件译文读取 `translation/events.json`，不再限于开场 JSON。补字编码固定表为 `translation/glyph_slots.zh-CN.json`，已保留全部 506 个候选字位。构建时读取工作目录中的原版资源与备份封装模板。

得到安装指令并正常退出游戏后，安装命令为：

```powershell
.\work\uv.ps1 run --locked python work/tools/install_alpha.py --stage alpha05
```

代理本轮没有运行安装命令。不要用资源导出命令覆盖已有译稿；译稿审阅页用 `.\work\uv.ps1 run --locked python work/tools/build_review.py` 重建。`merge_luna_drafts.py` 负责校验并合并本轮三个 Luna 输出批次，开始本轮时的译表、固定字位和工具备份在 `translation/luna_alpha05/baseline/`。


## 2026-10-09 三语工程统一重构

中、韩、英普通文字按稳定 ID 合并到 `translation/corpus.json`，当前中文从现行译稿回填，避免旧韩英对齐表中的 1,714 条旧中文覆盖新修改。术语与图片文字保留浅层主题表。图片原件、Unity 原版和字位约束集中到 `assets/sources/`；公共构建器解除了对旧测试版脚本和翻译任务目录的依赖。

历史批次、调查、版本资源和旧工具使用现有 `storage` 扩展的历史包管理，保留完整字节、原路径与逐文件哈希。目录结构说明和环境说明并入首页与 `tools/README.md`；原文档留在维护快照中。本轮没有安装或启动游戏，实际安装和当前候选继续由各自记录确定。完成数值与回归结果见 `maintenance/refactor-completion.json`。

## 2026-10-14 补丁器与分发载荷

新增 `patcher/`（Go，仅标准库）：读取差异载荷、核对游戏文件身份、备份、回放差异、原子替换，并可随时恢复原版；不带参数运行进入交互向导，已安装时直接提供恢复与语言切换。差异对象复用 `version_storage` 的 4 KiB 块格式，实测载荷约 23 MiB，而补丁后文件约 375 MiB。`tools/package_release.py` 与 `localize.py package-release` 把候选打包成载荷，并可整合补丁器生成终端用户压缩包；重复打包同一候选得到字节一致的结果。`.github/workflows/release.yml` 在拉取请求时执行静态检查、单元测试与两个平台构建，推送 tag 或手动确认时创建 Release 并上传可执行文件与 `SHA256SUMS`；含游戏数据的载荷由私有工作区生成后上传到同一个 Release。

回放实现对照 `version_storage.replay` 逐条核对：记录必须有序且不越界，从原版文件补空档前要先定位到输出位置（顺序读会让多记录文件重建错位），重建长度与 SHA-256 都要与对象名一致。切换语言时以原版备份为基准回放，不把差异叠在另一版补丁上；写入前尝试以可写方式打开目标文件，Windows 上运行中的游戏会锁住镜像文件。

## 2026-10-14 补丁器改为单文件四按钮形态

图形界面按实际使用场景重做：补丁在构建时经 `go:embed` 内嵌，发布物是单个可执行文件；把程序放进游戏根目录双击运行，窗口只有简体中文、韩文、English、还原原版四个按钮，点语言按钮安装，点还原恢复原版。游戏目录由程序所在目录推断，放错地方时按钮全部禁用并列出缺少的文件。原先的目录选择、补丁浏览、语言下拉、文件清单、拖放全部移除，相关能力保留在命令行子命令里。

新增 `tools/build_patcher.py` 与 `localize.py build-patcher`：把 `work/release/farland-patch-<候选>.zip` 复制进 `patcher/payload/`（缺失的候选自动先打包）再交叉编译，默认内嵌中、韩、英三个候选，产物约 75 MB，其中补丁数据约 71 MB。`patcher/payload/*.zip` 属私有工作区产物，已加入 `.gitignore`。内嵌加载逻辑放在平台中立的 `embedded.go`，Linux 上也能实测三个包可解析、顺序固定、清单合法。

同时修掉一个会让界面永久卡死的缺陷：工作线程回传结果时若世代已变就直接返回，不解除忙碌状态，而忙碌时所有按钮禁用、关闭窗口也被拦截。启动时只要状态检查跑完前发生一次失焦或新操作，忙碌就永远卡在 true。改为递增令牌管理，每次操作领取令牌、只归还自己的令牌，被作废的操作也照样归还；另加 3 分钟看门狗兜底。启动路径也改为只读安装记录（一个小 JSON），不再一上来就对六个文件共约 375 MB 做全量哈希。

韩文与英文候选的构建报告是早期格式，缺少 `language` 字段导致无法打包，已补齐并把版本名规范为候选目录名。端到端复验：合成游戏目录后依次安装中文、切换韩文、切换英文、还原、重复安装中文、最终还原，六个目标文件的 SHA-256 每一步都与 manifest 声明一致。

## 2026-10-15 界面定时器缺陷：WM_CREATE 期间设置只触发一次

实测发现窗口"无响应"的真正原因之一：`SetTimer` 原先在 `onCreate` 里调用，而 `onCreate` 由 `CreateWindowEx` 同步派发，也就是在 `CreateWindowEx` 内部执行。此时设置定时器，Windows 只投递一次 `WM_TIMER` 就再也不响。定时器是界面事件队列（`post`/`drainEvents`）唯一的驱动者，它一停，安装与还原完成后回传的闭包再也执行不到：状态行停在"正在安装…"，按钮永远禁用，关闭窗口也被拦下，用户看到的就是整个程序卡死。

把 `procSetTimer` 移到 `init()` 中 `CreateWindowEx` 返回之后，定时器持续每 50 毫秒触发，事件队列恢复正常。定位手段是给启动路径加了可选的跟踪日志：设环境变量 `FARLAND_PATCHER_TRACE` 后，程序把关键步骤与限流后的消息类型写进临时目录的 `farland-patcher-trace.log`，正常使用不写任何文件。跟踪显示启动全程 146 毫秒完成、主线程停在 `GetMessage` 等待，只有一条 `WM_TIMER`，据此定位到定时器。

同一轮还确认了两点：`loadEmbedded` 读入七十多兆内嵌补丁必须放在工作线程，否则界面线程被占住会超过 Windows 的响应阈值；`Get-Process.Responding` 与 `SendMessageTimeout` 对由 WSL interop 启动的进程判定不可靠，会稳定报告"无响应"，判断界面是否真的卡住要以程序内部的跟踪日志与消息流为准。
