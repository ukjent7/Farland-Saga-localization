//go:build !windows

package main

import "errors"

// guiAvailable 报告当前平台是否带图形界面。Windows 版用 Win32 实现，
// 其他平台只有控制台向导。
func guiAvailable() bool { return false }

func runGUI() error {
	return errors.New("图形界面只在 Windows 版提供；本平台请用交互向导或命令行子命令")
}
