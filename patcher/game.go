// 两款作品的补丁器共用同一套引擎：安装、回放、校验、备份和界面完全相同，
// 差异只有产品名、文件名、载荷格式和可执行文件里的表地址。
//
// 这些差异集中在本文件与 game_saga1.go / game_saga2.go 中，由构建标签选择一份：
// 默认构建一代，`-tags saga2` 构建二代。引擎其余文件不出现按游戏分支的代码。
//
// 补丁载荷目录同样按标签划分（payload/saga1、payload/saga2），go:embed 只嵌入
// 与当前构建对应的那一份；go:embed 的路径必须是字面量，所以两个 embedded_saga*.go
// 各写一条指令。
//
// 二代独有的 Format 3 扇区回放集中在 payload_saga2.go / cdrom_saga2.go，
// 安装器通过 replay_saga1.go / replay_saga2.go 的 replayEntry 调用，引擎本身
// 不知道两代回放方式的差别。
package main

// 各游戏配置文件必须提供下列常量与变量，类型与含义在此说明：
//
//	appTitle       窗口标题与帮助首行
//	gameEnvVar     指定游戏目录的环境变量名
//	gameExecutable 游戏主程序文件名，运行时用于提取 SSF 与光盘表
//	gameDiscImage  光盘镜像文件名
//	recordKind     安装记录里的作品标识
//	recordName     安装记录文件名
//	backupDirName  固定的原版备份目录名
//	rollbackDirName 写入失败时的回滚暂存目录名
//	stalePrefix    旧版带时间戳备份目录的前缀
//	rollbackPrefix 旧版带时间戳回滚目录的前缀
//	tempSuffix     写入临时文件的后缀
//	patcherStem    配置目录与配置文件名前缀
//	guiClassName   注册的窗口类名
//	traceFileName  诊断日志文件名
//	payloadFormats 该作品接受的 manifest 格式
//	ssfTableVA / ssfRawVA                 SSF 容器表在可执行文件里的虚拟地址
//	discLeftVA / discRightVA / discBlobVA 二代光盘置换表的虚拟地址（仅 saga2）
