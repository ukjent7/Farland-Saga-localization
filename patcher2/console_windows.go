//go:build windows

package main

import "syscall"

// enableUTF8Console 让 Windows 控制台按 UTF-8 解析输出，否则中文会变乱码。
// Windows Terminal 与较新的 conhost 都支持；失败也不影响功能。
func enableUTF8Console() {
	kernel32 := syscall.NewLazyDLL("kernel32.dll")
	if proc := kernel32.NewProc("SetConsoleOutputCP"); proc != nil {
		proc.Call(65001)
	}
}
