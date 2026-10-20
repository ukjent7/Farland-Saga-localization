// Saturn CD-ROM MODE1 扇区的 EDC/ECC 计算。
//
// 对应 fls/disc/cdrom.py 中的 make_tables / finish_sector。二代回放还要用到
// 扇区变换与可执行文件里的置换表，见 cdrom_saga2.go。
package main

import (
	"encoding/binary"
	"errors"
)

var (
	edcTable      [256]uint32
	eccForward    [256]byte
	eccBackward   [256]byte
	cdTablesReady bool
)

func initCDTables() {
	if cdTablesReady {
		return
	}
	for i := 0; i < 256; i++ {
		v := uint32(i)
		for j := 0; j < 8; j++ {
			if v&1 != 0 {
				v = (v >> 1) ^ 0xD8018001
			} else {
				v >>= 1
			}
		}
		edcTable[i] = v

		b := byte(i)
		f := (b << 1)
		if b&128 != 0 {
			f ^= 0x1D
		}
		eccForward[i] = f
		eccBackward[b^f] = b
	}
	cdTablesReady = true
}

func init() {
	initCDTables()
}

// ComputeEDC 计算 MODE1 扇区前 2064 字节的 EDC (32-bit CRC)。
func ComputeEDC(data []byte) uint32 {
	initCDTables()
	var v uint32
	for _, b := range data {
		v = (v >> 8) ^ edcTable[(byte(v)^b)&0xFF]
	}
	return v
}

// FinishSector 计算并填充 2352 字节 MODE1 扇区的 EDC 和 P/Q ECC 校验位。
func FinishSector(sector []byte) error {
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

	// 2. 8 字节零填充
	for i := 2068; i < 2076; i++ {
		sector[i] = 0
	}

	// 3. P 校验与 Q 校验
	parity := func(majors, minors, multiplier, increment, dest int) {
		size := majors * minors
		for major := 0; major < majors; major++ {
			index := (major>>1)*multiplier + (major & 1)
			var a, b byte
			for step := 0; step < minors; step++ {
				val := sector[12+index]
				index = (index + increment) % size
				a = eccForward[a^val]
				b ^= val
			}
			a = eccBackward[eccForward[a]^b]
			sector[dest+major] = a
			sector[dest+major+majors] = a ^ b
		}
	}

	// P Parity: 86 x 24, dest 2076
	parity(86, 24, 2, 86, 2076)
	// Q Parity: 52 x 43, dest 2248
	parity(52, 43, 86, 88, 2248)

	return nil
}
