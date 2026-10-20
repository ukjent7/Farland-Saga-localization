// SSF Steam 容器加解密算法。
//
// 对应 steam_container.py 中的 checksum、parameters 与 transform。
package main

import (
	"debug/pe"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"os"
)

// SSFParameters 保存 SSF 容器加解密所需的运行时参数。
type SSFParameters struct {
	Header       []byte
	Table        []byte
	QTable       [32]uint64
	A            uint64
	B            uint64
	TailKey      byte
	FilenameHash uint64
	HeaderHash   uint64
}

// SSFChecksum 计算 SSF 专用的 64 位校验和。
func SSFChecksum(data []byte, words bool) uint64 {
	var first, second uint32 = 1, 1
	var third uint32
	pos := 0
	if words {
		limit := (len(data) / 4) * 4
		for pos < limit {
			val := binary.LittleEndian.Uint32(data[pos : pos+4])
			first = first + val
			second = second - first
			pos += 4
		}
	}
	for i, val := range data[pos:] {
		v := uint32(val) | ((uint32(val) + uint32(i)) << 8)
		first = first + v
		second = second - first
		third = third + first*second
	}
	return ((uint64(second) | (uint64(first) << 32)) ^ (uint64(third) << 16))
}

// ReadPEVirtualAddress 从 PE 文件的指定虚拟地址读取指定字节数的数据。
func ReadPEVirtualAddress(file *pe.File, va uint64, count int) ([]byte, error) {
	var imageBase uint64
	switch opt := file.OptionalHeader.(type) {
	case *pe.OptionalHeader64:
		imageBase = opt.ImageBase
	case *pe.OptionalHeader32:
		imageBase = uint64(opt.ImageBase)
	default:
		return nil, errors.New("不支持的 PE 可选头格式")
	}

	rva := uint32(va - imageBase)
	for _, sec := range file.Sections {
		if rva >= sec.VirtualAddress && rva < sec.VirtualAddress+sec.VirtualSize {
			offset := rva - sec.VirtualAddress
			data := make([]byte, count)
			reader := sec.Open()
			if seeker, ok := reader.(io.Seeker); ok {
				if _, err := seeker.Seek(int64(offset), io.SeekStart); err != nil {
					return nil, err
				}
				if _, err := io.ReadFull(reader, data); err != nil {
					return nil, err
				}
				return data, nil
			}
			raw, err := sec.Data()
			if err != nil {
				return nil, err
			}
			if int(offset)+count > len(raw) {
				return nil, errors.New("超出节区数据范围")
			}
			copy(data, raw[offset:offset+uint32(count)])
			return data, nil
		}
	}
	return nil, fmt.Errorf("未找到包含 VA 0x%X 的节区", va)
}

// ExtractSSFTables 从当前作品的可执行文件提取 SSF 加密参数表。
func ExtractSSFTables(exePath string) (table []byte, raw []byte, perm []byte, key uint64, err error) {
	f, err := pe.Open(exePath)
	if err != nil {
		return nil, nil, nil, 0, fmt.Errorf("无法打开游戏可执行文件 %s: %w", exePath, err)
	}
	defer f.Close()

	table, err = ReadPEVirtualAddress(f, ssfTableVA, 1024)
	if err != nil {
		return nil, nil, nil, 0, fmt.Errorf("读取 SSF table 失败: %w", err)
	}
	raw, err = ReadPEVirtualAddress(f, ssfRawVA, 1024)
	if err != nil {
		return nil, nil, nil, 0, fmt.Errorf("读取 SSF raw 失败: %w", err)
	}

	perm = make([]byte, 256)
	seen := make(map[byte]bool, 256)
	for i := 0; i < 256; i++ {
		v := binary.LittleEndian.Uint32(raw[i*4 : (i+1)*4])
		p := byte((v + (v >> 24)) & 0xFF)
		perm[i] = p
		seen[p] = true
	}
	if len(seen) != 256 {
		return nil, nil, nil, 0, errors.New("SSF 置换表校验失败")
	}

	pass := []byte("V6pwsNFG")
	for _, c := range pass {
		ones := 0
		for b := c; b > 0; b >>= 1 {
			if b&1 != 0 {
				ones++
			}
		}
		bit := (ones & 1) << 7
		key = (key << 8) | uint64(c^byte(127+bit))
	}
	return table, raw, perm, key, nil
}

// LoadSSFParameters 解析指定 SSF 容器文件（如 Texture.tex、光盘镜像）的加密参数。
func LoadSSFParameters(sourcePath, exePath, logicalName string) (*SSFParameters, error) {
	table, raw, perm, key, err := ExtractSSFTables(exePath)
	if err != nil {
		return nil, err
	}

	header := make([]byte, 2048)
	f, err := os.Open(sourcePath)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	if _, err := io.ReadFull(f, header); err != nil {
		return nil, fmt.Errorf("读取容器头部失败: %w", err)
	}

	fnHash := SSFChecksum([]byte(logicalName), false)
	var selector, payloadSelector int
	for _, bit := range []uint64{2, 0x2000, 0x800000} {
		if fnHash&bit != 0 {
			selector++
		}
	}
	for _, bit := range []uint64{8, 0x8000, 0x80000} {
		if fnHash&bit != 0 {
			payloadSelector++
		}
	}

	reordered := make([]byte, 256)
	for i, dest := range perm {
		reordered[dest] = header[i]
	}
	hdrHash := SSFChecksum(reordered, true)

	rawQwords := make([]uint64, 128)
	for i := 0; i < 128; i++ {
		rawQwords[i] = binary.LittleEndian.Uint64(raw[i*8 : (i+1)*8])
	}
	a := rawQwords[fnHash&127] ^ fnHash
	hdrXor := binary.LittleEndian.Uint64(reordered[248:256]) ^ (a + rawQwords[a&127])

	rawDwords := make([]uint32, 256)
	for i := 0; i < 256; i++ {
		rawDwords[i] = binary.LittleEndian.Uint32(raw[i*4 : (i+1)*4])
	}
	decoded := make([]byte, 248)
	for i := 0; i < 31; i++ {
		val := binary.LittleEndian.Uint64(reordered[i*8 : (i+1)*8])
		res := val ^ uint64(rawDwords[i]) ^ hdrXor
		binary.LittleEndian.PutUint64(decoded[i*8:(i+1)*8], res)
	}

	expected := table[selector*256 : selector*256+248]
	for i := range decoded {
		if decoded[i] != expected[i] {
			return nil, errors.New("容器头部认证失败：exe 与文件名不匹配")
		}
	}

	var qtable [32]uint64
	qStart := payloadSelector * 256
	for i := 0; i < 32; i++ {
		qtable[i] = binary.LittleEndian.Uint64(table[qStart+i*8 : qStart+(i+1)*8])
	}

	var tailKey byte
	for i := 0; i < 8; i++ {
		tailKey += byte(hdrHash >> (i * 8))
	}

	return &SSFParameters{
		Header:       header,
		Table:        table,
		QTable:       qtable,
		A:            fnHash + key,
		B:            hdrHash - fnHash - key,
		TailKey:      tailKey,
		FilenameHash: fnHash,
		HeaderHash:   hdrHash,
	}, nil
}

// TransformSSF 对数据块执行 SSF 加密（encode=true）或解密（encode=false）。
func TransformSSF(block []byte, params *SSFParameters, encode bool) []byte {
	full := (len(block) / 2048) * 2048
	out := make([]byte, len(block))
	a, b, qt := params.A, params.B, params.QTable

	for sector := 0; sector < full; sector += 2048 {
		chain := b
		for i := 0; i < 256; i++ {
			pos := sector + i*8
			val := binary.LittleEndian.Uint64(block[pos : pos+8])
			if encode {
				cipher := ((val + a) ^ chain) + qt[i&31]
				binary.LittleEndian.PutUint64(out[pos:pos+8], cipher)
				chain = cipher + b
			} else {
				plain := (chain ^ (val - qt[i&31])) - a
				binary.LittleEndian.PutUint64(out[pos:pos+8], plain)
				chain = val + b
			}
		}
	}

	chain := uint32(params.TailKey)
	for i := full; i < len(block); i++ {
		val := block[i]
		idx := (i - full) & 1023
		tableVal := params.Table[idx]
		if encode {
			cipher := (val ^ byte(chain&0xFF)) + tableVal
			out[i] = cipher
			chain = uint32(params.TailKey) + uint32(cipher)
		} else {
			out[i] = byte(chain&0xFF) ^ (val - tableVal)
			chain = uint32(params.TailKey) + uint32(val)
		}
	}
	return out
}
