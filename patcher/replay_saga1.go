//go:build !saga2

// 一代的目标文件重建入口。一代的补丁只携带差异块，扇区回放是二代格式。
package main

import (
	"errors"
	"io"
)

// supportsSectors 报告当前作品是否接受带扇区对象的 manifest 条目。
func supportsSectors() bool {
	return false
}

// replayEntry 重建一个目标文件。exePath 在一代用不到，忽略。
func (p *payload) replayEntry(entry fileEntry, basePath string, _ string, output io.Writer, progress func(int64)) error {
	if entry.SectorsObject != "" {
		return errors.New("一代补丁不支持扇区对象")
	}
	return p.replay(entry, basePath, output, progress)
}
