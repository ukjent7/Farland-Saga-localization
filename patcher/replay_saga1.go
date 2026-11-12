//go:build !saga2

// 一代的目标文件重建入口。安装计划来源由 payload 的 recipe 字段接管；
// ZIP 载荷（--payload 外部排障包）直接回放差异块，扇区回放是二代格式。
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
	if p.recipe != nil {
		return p.recipe.replayEntry(entry, basePath, "", output, progress)
	}
	if entry.SectorsObject != "" {
		return errors.New("一代补丁不支持扇区对象")
	}
	return p.replay(entry, basePath, output, progress)
}
