// Steam Saturn 工具链使用的 LZ 打包与解包。
//
// 算法与 fls/disc/disc_decode.unpack_lz / fls.containers.overlay_font.pack_lz
// 逐字节一致：打包含三个大端 u32 头（解压大小、token 数、字面流偏移），
// token 流每 8 个一个标志字节，字面流尾随。包必须能在干净检出上重编译出
// 相同字节，一代的 overlay 字体操作依赖这一点。
package main

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
)

// UnpackLZ 解压可执行文件里以 LZ 压缩保存的表数据。
func UnpackLZ(data []byte) ([]byte, error) {
	if len(data) < 12 {
		return nil, errors.New("LZ 数据过短")
	}
	size := binary.BigEndian.Uint32(data[0:4])
	tokens := binary.BigEndian.Uint32(data[4:8])
	literalOffset := binary.BigEndian.Uint32(data[8:12])

	control := 12
	literal := int(literalOffset)
	output := make([]byte, 0, size)
	var mask byte
	var flags byte

	for i := uint32(0); i < tokens; i++ {
		if mask == 0 {
			if control >= len(data) {
				return nil, errors.New("LZ 控制流越界")
			}
			flags = data[control]
			control++
			mask = 128
		}
		if flags&mask != 0 {
			if literal+1 >= len(data) {
				return nil, errors.New("LZ 引用流越界")
			}
			distance := int(data[literal])
			count := int(data[literal+1]) + 3
			literal += 2
			if distance == 0 || distance > len(output) {
				return nil, errors.New("无效的 LZ 回溯距离")
			}
			for c := 0; c < count; c++ {
				output = append(output, output[len(output)-distance])
			}
		} else {
			if literal >= len(data) {
				return nil, errors.New("LZ 字面流越界")
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

// PackLZ 用与 Python pack_lz 相同的贪心策略压缩：对每个位置先找长度 3 的
// 最近匹配，再二分加长到最长可得长度，最后取该长度下最近（rfind）的匹配。
// 匹配允许与当前位置重叠，回溯距离不得超过 255。输出与 Python 实现逐字节
// 一致，这是 overlay 字体操作保持补丁后哈希不变的前提。
func PackLZ(data []byte) ([]byte, error) {
	var flags, payload []byte
	pos, tokens, flag, flagBits := 0, 0, byte(0), 0
	size := len(data)
	for pos < size {
		maximum := min(258, size-pos)
		start := max(0, pos-255)
		found := -1
		if maximum >= 3 {
			found = indexLast(data, start, pos+2, data[pos:pos+3])
		}
		count := 1
		if found >= 0 {
			low, high := 3, maximum
			for low < high {
				mid := (low + high + 1) / 2
				if indexLast(data, start, pos+mid-1, data[pos:pos+mid]) >= 0 {
					low = mid
				} else {
					high = mid - 1
				}
			}
			count = low
			found = indexLast(data, start, pos+count-1, data[pos:pos+count])
			distance := pos - found
			if distance <= 0 || distance > 255 {
				return nil, fmt.Errorf("LZ 回溯距离越界于偏移 %d: %d", pos, distance)
			}
			payload = append(payload, byte(distance), byte(count-3))
			flag |= 1 << (7 - flagBits)
		} else {
			payload = append(payload, data[pos])
		}
		pos += count
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
	binary.BigEndian.PutUint32(header[4:8], uint32(tokens))
	binary.BigEndian.PutUint32(header[8:12], uint32(12+len(flags)))
	out := append(header, flags...)
	return append(out, payload...), nil
}

// indexLast 在 data[low:high] 里找 needle 的最后一次出现，返回**绝对**
// 索引；区间放不下 needle 时返回 -1，与 Python bytes.rfind(needle, low, high)
// 的语义一致。
func indexLast(data []byte, low, high int, needle []byte) int {
	if low < 0 || high > len(data) || high-low < len(needle) {
		return -1
	}
	found := bytes.LastIndex(data[low:high], needle)
	if found < 0 {
		return -1
	}
	return low + found
}
