//go:build saga2

// 二代专用的光盘镜像重建：Steam Saturn 扇区变换、可执行文件里的置换表，
// 以及零 ECC 扇区。一代补丁只回放预计算的差异块，用不到这些。
package main

import (
	"debug/pe"
	"encoding/binary"
	"errors"
	"fmt"
)

// FinishSectorZeroECC 计算并填充 2352 字节 MODE1 扇区的 EDC，ECC 部分填 0。
// 二代 Steam 镜像的扇区就这样存储，回放时按同样形态生成再加密。
func FinishSectorZeroECC(sector []byte) error {
	if len(sector) != 2352 {
		return errors.New("扇区长度必须为 2352 字节")
	}
	if sector[15] != 1 {
		return errors.New("仅支持 MODE1 扇区")
	}
	initCDTables()

	// 1. EDC
	edc := ComputeEDC(sector[:2064])
	binary.LittleEndian.PutUint32(sector[2064:2068], edc)

	// 2. 8 字节零填充 + 276 字节 ECC 零填充
	for i := 2068; i < 2352; i++ {
		sector[i] = 0
	}
	return nil
}

// SectorTransform 对 2352 字节的扇区执行 Steam Saturn 镜像变换（加密或解密）。
func SectorTransform(data []byte, lba int, dwords []uint32, qwords []uint64, encode bool) []byte {
	idx := lba & 1023
	nxt := (idx + 1) & 1023
	chain := uint64(dwords[idx]) | (uint64(dwords[nxt]) << 32)
	idx = nxt >> 1

	output := make([]byte, 2352)
	for i := 0; i < 294; i++ {
		idx = (idx + 1) & 511
		val := binary.LittleEndian.Uint64(data[i*8 : (i+1)*8])
		if encode {
			val = val + chain
			binary.LittleEndian.PutUint64(output[i*8:(i+1)*8], val)
			chain = val + qwords[idx]
		} else {
			binary.LittleEndian.PutUint64(output[i*8:(i+1)*8], val-chain)
			chain = val + qwords[idx]
		}
	}
	return output
}

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

// DiscTables 保存扇区混淆与置换表。
type DiscTables struct {
	Dwords      []uint32
	Qwords      []uint64
	Permutation []byte
}

// ExtractDiscTables 从游戏可执行文件提取扇区变换与 LBA 置换表。
func ExtractDiscTables(exePath string) (*DiscTables, error) {
	f, err := pe.Open(exePath)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	leftRaw, err := ReadPEVirtualAddress(f, discLeftVA, 4096)
	if err != nil {
		return nil, err
	}
	rightRaw, err := ReadPEVirtualAddress(f, discRightVA, 4096)
	if err != nil {
		return nil, err
	}

	left := make([]uint32, 1024)
	right := make([]uint32, 1024)
	for i := 0; i < 1024; i++ {
		left[i] = binary.LittleEndian.Uint32(leftRaw[i*4 : (i+1)*4])
		right[i] = binary.LittleEndian.Uint32(rightRaw[i*4 : (i+1)*4])
	}

	initial := make([]byte, 4096)
	for i := 0; i < 1024; i++ {
		binary.LittleEndian.PutUint32(initial[i*4:(i+1)*4], left[i]-right[i])
	}

	var key uint64
	pass := []byte("jS6ftnRc")
	for _, c := range pass {
		ones := 0
		for b := c; b > 0; b >>= 1 {
			if b&1 != 0 {
				ones++
			}
		}
		var xorVal byte = 247
		if ones&1 != 0 {
			xorVal = 126
		}
		key = (key << 8) | uint64(c^xorVal)
	}
	swapped := (key & 0xFFFFFFFF) * 0x100000001

	cipherTable := make([]byte, 4096)
	for i := 0; i < 512; i++ {
		v := binary.LittleEndian.Uint64(initial[i*8 : (i+1)*8])
		binary.LittleEndian.PutUint64(cipherTable[i*8:(i+1)*8], v^swapped)
	}

	dwords := make([]uint32, 1024)
	for i := 0; i < 1024; i++ {
		dwords[i] = binary.LittleEndian.Uint32(cipherTable[i*4 : (i+1)*4])
	}
	qwords := make([]uint64, 512)
	for i := 0; i < 512; i++ {
		qwords[i] = binary.LittleEndian.Uint64(cipherTable[i*8 : (i+1)*8])
	}

	compressed, err := ReadPEVirtualAddress(f, discBlobVA, 0x300000)
	if err != nil {
		return nil, err
	}
	delta, err := UnpackLZ(compressed)
	if err != nil {
		return nil, err
	}

	perm := make([]byte, len(delta))
	for i, v := range delta {
		perm[i] = byte((int(v) + (i & 63)) & 255)
	}

	return &DiscTables{
		Dwords:      dwords,
		Qwords:      qwords,
		Permutation: perm,
	}, nil
}
