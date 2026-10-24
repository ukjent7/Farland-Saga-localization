//go:build saga2

// 1bpp text resources and 8-bit character cards both embed already prepared
// bytes in the plan, so the installer only copies each blob range into the
// pristine resource. Layout stays in the recipe compiler.
package main

import (
	"errors"
	"fmt"
)

func applyMonoBitmapOperations(original []byte, operations []recipeOperation, blob []byte) ([]byte, error) {
	return applyWrites(original, operations, blob, "mono_bitmap")
}

// applyTilemapWrites copies the compiled tile map and tile graphics into an
// 8-bit card resource. The original header, palette and unused tail stay.
func applyTilemapWrites(original []byte, operations []recipeOperation, blob []byte) ([]byte, error) {
	return applyWrites(original, operations, blob, "tilemap_writes")
}

// applyPlateOperations copies one 72x48 palette-index nameplate over the
// pristine image. The engine blits the canvas verbatim, so nothing else in the
// resource needs touching; the 640-byte tail stays as shipped.
func applyPlateOperations(original []byte, operations []recipeOperation, blob []byte) ([]byte, error) {
	return applyWrites(original, operations, blob, "plate_bitmap")
}

func applyWrites(original []byte, operations []recipeOperation, blob []byte, kind string) ([]byte, error) {
	out := append([]byte(nil), original...)
	for _, operation := range operations {
		if operation.Type != kind {
			return nil, fmt.Errorf("不支持的写入操作 %q", operation.Type)
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
		return nil, errors.New("资源大小发生变化")
	}
	return out, nil
}
