//go:build !windows

package main

// enableUTF8Console 在非 Windows 平台无需处理，终端默认按 UTF-8 输出。
func enableUTF8Console() {}
