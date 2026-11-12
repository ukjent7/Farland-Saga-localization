// 两代共用的光盘镜像扇区机制：Steam Saturn 扇区变换、可执行文件里的置换表。
// 置换表的虚拟地址按游戏定义在 game_saga1.go / game_saga2.go，两个引擎的
// 密钥推导与表结构完全一致。LZ 表解包在共享的 lz.go。
package main

import (
	"debug/pe"
	"encoding/binary"
)

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
