//go:build saga2

// 二代测试用的工作区路径：二代没有纳入本仓库的固定备份，直接读安装目录
// 里的原版文件。FARLAND_SAGA2_TEST_DIR 可指向另一份干净副本，避免本地
// Steam 目录已被旧补丁改写时测试只能跳过。
package main

import (
	"os"
	"path/filepath"
)

const referenceBackup = ""

var referenceGameDir = func() string {
	if value := os.Getenv("FARLAND_SAGA2_TEST_DIR"); value != "" {
		return value
	}
	return "/mnt/e/Steam/steamapps/common/FARLAND SAGA II Saturn Tribute"
}()

// referenceExecutable 返回二代可执行文件的可选路径。
func referenceExecutable() string {
	return filepath.Join(referenceGameDir, gameExecutable)
}
