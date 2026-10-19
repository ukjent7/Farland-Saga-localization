package main

import (
	"bytes"
	"encoding/hex"
	"os"
	"path/filepath"
	"testing"
)

func TestCDROMEDCAndECC(t *testing.T) {
	trackPath := filepath.Join("..", "..", "work", "decoded", "FarlandSaga_track1.bin")
	data, err := os.ReadFile(trackPath)
	if err != nil {
		t.Skipf("测试工作区未找到 decoded track1，跳过物理扇区回读测试: %v", err)
		return
	}
	if len(data) < 2352 {
		t.Fatalf("track1 长度不足: %d", len(data))
	}

	sector := make([]byte, 2352)
	copy(sector, data[:2352])

	if err := FinishSector(sector); err != nil {
		t.Fatalf("FinishSector 失败: %v", err)
	}

	edcHex := hex.EncodeToString(sector[2064:2068])
	if edcHex != "f5148449" {
		t.Errorf("EDC 不符: 期望 f5148449, 实际 %s", edcHex)
	}

	eccPHex := hex.EncodeToString(sector[2076:2080])
	if eccPHex != "7d7ab3c0" {
		t.Errorf("ECC P 不符: 期望 7d7ab3c0, 实际 %s", eccPHex)
	}

	eccQHex := hex.EncodeToString(sector[2248:2252])
	if eccQHex != "ed969eae" {
		t.Errorf("ECC Q 不符: 期望 ed969eae, 实际 %s", eccQHex)
	}
}

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
