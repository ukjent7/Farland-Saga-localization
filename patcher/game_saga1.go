//go:build !saga2

// 一代（FARLAND SAGA I Saturn Tribute）的产品参数。默认构建这一份。
package main

const (
	appTitle        = "FARLAND SAGA I Saturn Tribute"
	gameEnvVar      = "FARLAND_SAGA_GAME"
	gameExecutable  = "FarlandSaga.exe"
	gameDiscImage   = "FarlandSaga.bin"
	recordKind      = "farland-saga-patcher-install"
	recordName      = "FarlandSagaPatcher-install.json"
	backupDirName   = "FarlandSagaPatcher-backup"
	rollbackDirName = "FarlandSagaPatcher-rollback"
	stalePrefix     = "FarlandSagaPatcher-backup-"
	rollbackPrefix  = "FarlandSagaPatcher-restore-rollback-"
	tempSuffix      = ".FarlandSagaPatcher-tmp"
	patcherStem     = "FarlandSagaPatcher"
	guiClassName    = "FarlandSagaPatcherWindow"
	traceFileName   = "farland-patcher-trace.log"
	payloadDir      = "payload/saga1"
)

// payloadFormats 是一代接受的 manifest 格式：声明式差异块（1）。
var payloadFormats = []int{1}

// 一代可执行文件里的加密表地址，见 ssf.go 与 cdrom.go。
const (
	ssfTableVA  uint64 = 0x1410B9B90
	ssfRawVA    uint64 = 0x1410B9F90
	discLeftVA  uint64 = 0x14118DDB0
	discRightVA uint64 = 0x14118EDB0
	discBlobVA  uint64 = 0x140D2D2D0
)
