//go:build saga2

// Script resources store speaker indices as big-endian u16 words. The
// restaurant scene names the cook through index 14, which the shell draws
// from a baked Japanese sprite; the recipe repoints those words at index 30
// so the translated name in the 02CB bank is drawn instead.
package main

import (
	"encoding/binary"
	"fmt"
)

func applyWordPatchOperations(original []byte, operations []recipeOperation) ([]byte, error) {
	out := append([]byte(nil), original...)
	for _, operation := range operations {
		if operation.Type != "word_patches" {
			return nil, fmt.Errorf("不支持的脚本词补丁操作 %q", operation.Type)
		}
		for _, patch := range operation.Patches {
			if patch.Offset < 0 || patch.Offset > len(out)-2 {
				return nil, fmt.Errorf("%s: 补丁越过资源", patch.Name)
			}
			current := int(binary.BigEndian.Uint16(out[patch.Offset : patch.Offset+2]))
			if current != patch.Original {
				return nil, fmt.Errorf("%s: 原值不符：期望 %d，实际 %d",
					patch.Name, patch.Original, current)
			}
			binary.BigEndian.PutUint16(out[patch.Offset:patch.Offset+2], uint16(patch.Patched))
		}
	}
	if len(out) != len(original) {
		return nil, fmt.Errorf("资源大小发生变化")
	}
	return out, nil
}
