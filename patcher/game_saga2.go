//go:build saga2

// 二代（FARLAND SAGA II Saturn Tribute）的产品参数。用 -tags saga2 构建这一份。
package main

const (
	appTitle        = "FARLAND SAGA II Saturn Tribute"
	gameEnvVar      = "FARLAND_SAGA_GAME"
	gameExecutable  = "FarlandSaga2.exe"
	gameDiscImage   = "FarlandSaga2.bin"
	recordKind      = "farland-saga-2-patcher-install"
	recordName      = "FarlandSaga2Patcher-install.json"
	backupDirName   = "FarlandSaga2Patcher-backup"
	rollbackDirName = "FarlandSaga2Patcher-rollback"
	stalePrefix     = "FarlandSaga2Patcher-backup-"
	rollbackPrefix  = "FarlandSaga2Patcher-restore-rollback-"
	tempSuffix      = ".FarlandSaga2Patcher-tmp"
	patcherStem     = "FarlandSaga2Patcher"
	guiClassName    = "FarlandSaga2PatcherWindow"
	traceFileName   = "farland-saga-2-patcher-trace.log"
)

// payloadFormats 是二代接受的 manifest 格式：声明式差异块（1）与
// 纯扇区回放（3）。二代发布版使用 3，可执行文件自身的差异仍用 1。
var payloadFormats = []int{1, 3}

// 二代可执行文件里的加密表地址，见 ssf.go 与 cdrom.go。
const (
	ssfTableVA  uint64 = 0x1410F8770
	ssfRawVA    uint64 = 0x1410F8B70
	discLeftVA  uint64 = 0x1411CCDD0
	discRightVA uint64 = 0x1411CDDD0
	discBlobVA  uint64 = 0x140D6BE88
)
