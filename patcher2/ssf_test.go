package main

import (
	"bytes"
	"io"
	"os"
	"path/filepath"
	"testing"
)

func TestSSFChecksum(t *testing.T) {
	name := "Texture.tex"
	fnHash := SSFChecksum([]byte(name), false)
	expected := uint64(0x485d1628c7562)
	if fnHash != expected {
		t.Errorf("SSFChecksum 结果不符: 期望 0x%X, 实际 0x%X", expected, fnHash)
	}
}

func TestSSFDecryptTexture(t *testing.T) {
	exePath := filepath.Join("..", "..", "work", "analysis", "FarlandSaga2.exe")
	texPath := filepath.Join("..", "..", "work", "backups", "alpha01-20260930-104848", "Texture.tex")

	if _, err := os.Stat(exePath); err != nil {
		t.Skipf("工作区无分析 exe，跳过 SSF 测试: %v", err)
		return
	}
	if _, err := os.Stat(texPath); err != nil {
		t.Skipf("工作区无 Texture.tex，跳过 SSF 测试: %v", err)
		return
	}

	params, err := LoadSSFParameters(texPath, exePath, "Texture.tex")
	if err != nil {
		t.Fatalf("LoadSSFParameters 失败: %v", err)
	}

	f, err := os.Open(texPath)
	if err != nil {
		t.Fatalf("打开 Texture.tex 失败: %v", err)
	}
	defer f.Close()

	if _, err := f.Seek(2048, io.SeekStart); err != nil {
		t.Fatalf("Seek 失败: %v", err)
	}
	block := make([]byte, 2048)
	if _, err := io.ReadFull(f, block); err != nil {
		t.Fatalf("读取第一块失败: %v", err)
	}

	decrypted := TransformSSF(block, params, false)
	if !bytes.HasPrefix(decrypted, []byte("Texture ")) {
		t.Fatalf("解密第一块前缀不符合预期: %q", string(decrypted[:16]))
	}

	reEncoded := TransformSSF(decrypted, params, true)
	if !bytes.Equal(block, reEncoded) {
		t.Fatal("TransformSSF 重新加密未复现原始加密块")
	}
}
