//go:build saga2

// Story pages are two-sector 2bpp images with a voice timing table. The plan
// stores the rendered 196x48 image; the installer keeps the page's original
// timing curve and rescales it to the translated text length.
package main

import (
	"encoding/binary"
	"errors"
	"fmt"
	"math"
)

const (
	storyImageBytes    = 2352
	storyTimingOffset  = 2354
	storyTimingTable   = 2356
	storyTimingEnd     = 2528
	storyTimingEntries = (storyTimingEnd - storyTimingTable) / 4
)

func applyStoryPageOperations(original []byte, operations []recipeOperation, blob []byte) ([]byte, error) {
	out := append([]byte(nil), original...)
	for _, operation := range operations {
		if operation.Type != "story_page" {
			return nil, fmt.Errorf("不支持的剧情页操作 %q", operation.Type)
		}
		if len(out) < storyTimingEnd {
			return nil, errors.New("剧情页长度不足以容纳时序表")
		}
		if operation.DataOffset < 0 || operation.DataLength != storyImageBytes ||
			operation.DataOffset > len(blob) ||
			operation.DataLength > len(blob)-operation.DataOffset {
			return nil, errors.New("剧情页图像范围越过 plan")
		}
		copy(out[:storyImageBytes], blob[operation.DataOffset:operation.DataOffset+operation.DataLength])
		if err := rewriteStoryTiming(out, operation.TimingCharacters); err != nil {
			return nil, err
		}
	}
	if len(out) != len(original) {
		return nil, errors.New("剧情页大小发生变化")
	}
	return out, nil
}

// rewriteStoryTiming scales the original voice timing curve to a new character
// count, preserving the first and last timestamps. The arithmetic mirrors the
// Python reference, including round-half-to-even.
func rewriteStoryTiming(page []byte, count int) error {
	if len(page) < storyTimingEnd {
		return errors.New("剧情页长度不足以容纳时序表")
	}
	original := int(binary.BigEndian.Uint16(page[storyTimingOffset : storyTimingOffset+2]))
	if original <= 0 || count <= 0 {
		return nil
	}
	if original+1 > storyTimingEntries {
		return fmt.Errorf("原始时序字符数 %d 超出表容量", original)
	}
	if count+1 > storyTimingEntries {
		return fmt.Errorf("时序字符数 %d 超出表容量", count)
	}
	times := make([]uint32, original+1)
	for index := range times {
		times[index] = binary.BigEndian.Uint32(page[storyTimingTable+index*4:])
	}
	scaled := make([]uint32, count+1)
	for index := range scaled {
		position := float64(index) * float64(original) / float64(count)
		left := int(position)
		if left > original {
			left = original
		}
		right := left + 1
		if right > original {
			right = original
		}
		fraction := position - float64(left)
		value := float64(times[left])*(1-fraction) + float64(times[right])*fraction
		scaled[index] = uint32(math.RoundToEven(value))
	}
	binary.BigEndian.PutUint16(page[storyTimingOffset:storyTimingOffset+2], uint16(count))
	for index, value := range scaled {
		binary.BigEndian.PutUint32(page[storyTimingTable+index*4:], value)
	}
	for index := storyTimingTable + len(scaled)*4; index < storyTimingEnd; index++ {
		page[index] = 0
	}
	return nil
}
