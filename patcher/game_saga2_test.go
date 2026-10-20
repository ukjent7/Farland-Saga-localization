//go:build saga2

// 二代测试用的工作区路径：二代没有纳入本仓库的固定备份，直接读安装目录
// 里的原版文件。
package main

import "path/filepath"

const (
	referenceBackup  = ""
	referenceGameDir = "/mnt/e/Steam/steamapps/common/FARLAND SAGA II Saturn Tribute"
)

// referenceExecutable 返回二代可执行文件的可选路径。
func referenceExecutable() string {
	return filepath.Join(referenceGameDir, gameExecutable)
}
