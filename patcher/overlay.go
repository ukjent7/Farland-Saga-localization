// Steam 侧物品说明 RGBA 字体生成与 LZSS 压缩。
//
// 对应 overlay_font.py 中的 pack_lz / expand_font / font_patch。
package main

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
)

const (
	FontVA      uint64 = 0x140867cc0
	FontLimitVA uint64 = 0x1408d9350
)

// UnpackLZ 解压 Steam 版内部使用的 LZ 压缩数据块。
func UnpackLZ(data []byte) ([]byte, error) {
	if len(data) < 12 {
		return nil, errors.New("LZ 数据头部过短")
	}
	size := binary.BigEndian.Uint32(data[0:4])
	tokens := binary.BigEndian.Uint32(data[4:8])
	literalOffset := binary.BigEndian.Uint32(data[8:12])

	control := 12
	literal := int(literalOffset)
	output := make([]byte, 0, size)
	var mask byte

	for i := uint32(0); i < tokens; i++ {
		if mask == 0 {
			if control >= len(data) {
				return nil, errors.New("LZ 控制字节超出范围")
			}
			mask = 128
			control++
		}
		flag := data[control-1]
		if flag&mask != 0 {
			if literal+1 >= len(data) {
				return nil, errors.New("LZ 引用超出范围")
			}
			distance := int(data[literal])
			count := int(data[literal+1]) + 3
			literal += 2
			if distance == 0 || distance > len(output) {
				return nil, errors.New("无效的 LZ 回溯距离")
			}
			for step := 0; step < count; step++ {
				output = append(output, output[len(output)-distance])
			}
		} else {
			if literal >= len(data) {
				return nil, errors.New("LZ 字面量超出范围")
			}
			output = append(output, data[literal])
			literal++
		}
		mask >>= 1
	}

	if uint32(len(output)) != size {
		return nil, fmt.Errorf("LZ 解压大小不符: 期望 %d, 实际 %d", size, len(output))
	}
	return output, nil
}

// PackLZ 将原始数据进行 LZSS 压缩，格式与 Steam 游戏可执行文件完全一致。
func PackLZ(data []byte) []byte {
	size := len(data)
	flags := make([]byte, 0)
	payload := make([]byte, 0)
	pos := 0
	tokens := uint32(0)
	var flag byte
	flagBits := 0

	for pos < size {
		maximum := 258
		if size-pos < maximum {
			maximum = size - pos
		}
		start := pos - 255
		if start < 0 {
			start = 0
		}

		bestLen := 0
		bestDist := 0

		if maximum >= 3 {
			// 在 [start, pos) 查找最长匹配
			target := data[pos : pos+3]
			window := data[start:pos]
			idx := bytes.LastIndex(window, target)
			if idx >= 0 {
				low := 3
				high := maximum
				for low < high {
					mid := (low + high + 1) / 2
					t := data[pos : pos+mid]
					if bytes.LastIndex(data[start:pos+mid-1], t) >= 0 {
						low = mid
					} else {
						high = mid - 1
					}
				}
				bestLen = low
				found := bytes.LastIndex(data[start:pos+bestLen-1], data[pos:pos+bestLen])
				bestDist = pos - (start + found)
			}
		}

		if bestLen >= 3 && bestDist > 0 && bestDist <= 255 {
			payload = append(payload, byte(bestDist), byte(bestLen-3))
			flag |= 1 << (7 - flagBits)
			pos += bestLen
		} else {
			payload = append(payload, data[pos])
			pos++
		}

		tokens++
		flagBits++
		if flagBits == 8 {
			flags = append(flags, flag)
			flag = 0
			flagBits = 0
		}
	}

	if flagBits > 0 {
		flags = append(flags, flag)
	}

	header := make([]byte, 12)
	binary.BigEndian.PutUint32(header[0:4], uint32(size))
	binary.BigEndian.PutUint32(header[4:8], tokens)
	binary.BigEndian.PutUint32(header[8:12], uint32(12+len(flags)))

	result := make([]byte, 0, 12+len(flags)+len(payload))
	result = append(result, header...)
	result = append(result, flags...)
	result = append(result, payload...)
	return result
}

// ExpandFont 将 1bpp 单色字模（每个字符 16x16=32 字节）展开为 RGBA 格式（1024 字节/字符）。
func ExpandFont(mono []byte) []byte {
	result := make([]byte, len(mono)*32)
	for off, val := range mono {
		for bit := 0; bit < 8; bit++ {
			if val&(128>>bit) != 0 {
				result[off*32+bit*4+3] = 255
			}
		}
	}
	return result
}
