package main

import (
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
