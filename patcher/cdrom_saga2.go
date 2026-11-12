//go:build saga2

// 二代专用的光盘镜像重建：零 ECC 扇区。扇区变换与置换表在共享的 disc_ssf.go。
package main

import (
	"encoding/binary"
	"errors"
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
