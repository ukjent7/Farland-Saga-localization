//go:build !saga2

// 一代测试用的工作区路径：原版备份与可执行文件都由一代的工具链产生。
package main

import "path/filepath"

const (
	// referenceBackup 是一代原版文件的固定备份，取自首次完整安装之前。
	referenceBackup = "alpha01-20260930-104848"
	// referenceGameDir 是一代安装目录，备份缺文件时从这里补齐。
	referenceGameDir = ""
)

// referenceExecutable 返回一代可执行文件的可选路径（用于真实载荷回放）。
func referenceExecutable() string {
	return filepath.Join("..", "..", "work", "analysis", "FarlandSaga.exe")
}
