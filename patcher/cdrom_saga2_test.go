//go:build saga2

// 二代专用的扇区变换与置换表测试：需要二代的游戏可执行文件。
package main

import (
	"bytes"
	"os"
	"testing"
)

func TestSectorTransformRoundtrip(t *testing.T) {
	dwords := make([]uint32, 1024)
	qwords := make([]uint64, 512)
	for i := range dwords {
		dwords[i] = uint32(i*101 + 37)
	}
	for i := range qwords {
		qwords[i] = uint64(i*997 + 1013)
	}

	original := bytes.Repeat([]byte("SEGA SEGASATURN MODE1 SECTOR TEST VECTOR"), 60)[:2352]
	encoded := SectorTransform(original, 0, dwords, qwords, true)
	decoded := SectorTransform(encoded, 0, dwords, qwords, false)

	if !bytes.Equal(original, decoded) {
		t.Fatal("SectorTransform 编解码未能无损还原")
	}
}

func TestExtractDiscTables(t *testing.T) {
	exePath := "/mnt/e/Steam/steamapps/common/FARLAND SAGA II Saturn Tribute/FarlandSaga2.exe"
	if _, err := os.Stat(exePath); err != nil {
		t.Skipf("测试工作区未找到 FarlandSaga2.exe: %v", err)
		return
	}

	tables, err := ExtractDiscTables(exePath)
	if err != nil {
		t.Fatalf("ExtractDiscTables 失败: %v", err)
	}

	if len(tables.Dwords) != 1024 {
		t.Errorf("Dwords 长度不符: %d", len(tables.Dwords))
	}
	if len(tables.Qwords) != 512 {
		t.Errorf("Qwords 长度不符: %d", len(tables.Qwords))
	}
	if len(tables.Permutation) != 2270720 {
		t.Errorf("Permutation 长度不符: %d", len(tables.Permutation))
	}
}
