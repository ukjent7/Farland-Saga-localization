//go:build saga2

// 1bpp text resources: the plan stores the already packed slot bytes, so the
// installer only copies each blob range into the pristine resource. Packing a
// slot is layout work and stays in the recipe compiler.
package main

import (
	"errors"
	"fmt"
)

func applyMonoBitmapOperations(original []byte, operations []recipeOperation, blob []byte) ([]byte, error) {
	out := append([]byte(nil), original...)
	for _, operation := range operations {
		if operation.Type != "mono_bitmap" {
			return nil, fmt.Errorf("不支持的位图操作 %q", operation.Type)
		}
		for _, write := range operation.Writes {
			if write.Offset < 0 || write.DataLength <= 0 ||
				write.Offset > len(out) || write.DataLength > len(out)-write.Offset {
				return nil, fmt.Errorf("%s: 写入范围越过资源", write.Name)
			}
			if write.DataOffset < 0 || write.DataOffset > len(blob) ||
				write.DataLength > len(blob)-write.DataOffset {
				return nil, fmt.Errorf("%s: 写入数据范围越过 plan", write.Name)
			}
			copy(out[write.Offset:write.Offset+write.DataLength],
				blob[write.DataOffset:write.DataOffset+write.DataLength])
		}
	}
	if len(out) != len(original) {
		return nil, errors.New("位图资源大小发生变化")
	}
	return out, nil
}
