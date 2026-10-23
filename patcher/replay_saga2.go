//go:build saga2

// 二代的目标文件重建入口：镜像走现场扇区重建，可执行文件走差异块回放。
package main

import (
	"io"
)

// supportsSectors 报告当前作品是否接受带扇区对象的 manifest 条目。
func supportsSectors() bool {
	return true
}

// replayEntry 重建一个目标文件。exePath 为游戏可执行文件，扇区回放要从
// 它的 PE 映像里取加密表；留空时由 replaySectors 自行就近查找。
func (p *payload) replayEntry(entry fileEntry, basePath string, exePath string, output io.Writer, progress func(int64)) error {
	if p.recipe != nil {
		return p.recipe.replayEntry(entry, basePath, exePath, output, progress)
	}
	if entry.SectorsObject != "" {
		return p.replaySectors(entry, basePath, exePath, output, progress)
	}
	return p.replay(entry, basePath, output, progress)
}
