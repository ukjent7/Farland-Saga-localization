//go:build !saga2

package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// parseTextureDeclarationForTest 从 recipe/fs1/texture.json 提取各语言 PNG、
// 补丁后身份与共享的 Parts 元数据，供真实容器测试使用。
func parseTextureDeclarationForTest(data []byte) (map[string][]byte, map[string]string, []byte, error) {
	var doc struct {
		Schema    int    `json:"schema"`
		File      string `json:"file"`
		Languages map[string]struct {
			PNG          string `json:"png"`
			Suffix       string `json:"suffix"`
			PatchedSHA   string `json:"patched_sha256"`
			PatchedSize  int64  `json:"patched_size"`
		} `json:"languages"`
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil, nil, nil, err
	}
	if doc.Schema != 1 || doc.File != "Texture.tex" {
		return nil, nil, nil, errorsNew("结构不符")
	}
	pngs := map[string][]byte{}
	sizes := map[string]string{}
	var suffix []byte
	var err error
	for language, section := range doc.Languages {
		pngs[language], err = base64Decode(section.PNG)
		if err != nil {
			return nil, nil, nil, err
		}
		suffix, err = base64Decode(section.Suffix)
		if err != nil {
			return nil, nil, nil, err
		}
		sizes[language] = section.PatchedSHA
		_ = section.PatchedSize
	}
	return pngs, sizes, suffix, nil
}

// TestFS1TextureOpRealContainer 用真实原版 Texture.tex 验证 texture 操作：
// 从提交的 texture.json 里取各语言的 PNG，重建容器并核对声明的哈希。
func TestFS1TextureOpRealContainer(t *testing.T) {
	backup := os.Getenv("FARLAND_FS1_DISC_BACKUP")
	if backup == "" {
		t.Skip("需要 FARLAND_FS1_DISC_BACKUP 指向原版备份目录")
	}
	decl, err := os.ReadFile(filepath.Join("..", "recipe", "fs1", "texture.json"))
	if err != nil {
		t.Fatalf("读取 texture.json 失败: %v", err)
	}
	pngByLang, patchedByLang, suffix, err := parseTextureDeclarationForTest(decl)
	if err != nil {
		t.Fatalf("解析 texture.json 失败: %v", err)
	}
	exePath := filepath.Join(backup, "FarlandSaga.exe")
	original, err := os.ReadFile(filepath.Join(backup, "Texture.tex"))
	if err != nil {
		t.Fatalf("读取原版失败: %v", err)
	}
	for language, png := range pngByLang {
		op := fs1PlanOp{Type: "texture", Name: "localized_atlas",
			DataOffset: 0, DataLength: int64(len(png))}
		blob := append(append([]byte(nil), png...), suffix...)
		rebuilt, err := applyTextureOp(original, &op, &fs1PlanEntry{}, blob,
			filepath.Join(backup, "Texture.tex"), exePath)
		if err != nil {
			t.Fatalf("%s: 重建失败: %v", language, err)
		}
		got := sha256.Sum256(rebuilt)
		if hex.EncodeToString(got[:]) != patchedByLang[language] {
			t.Errorf("%s: 哈希不符: %s", language, hex.EncodeToString(got[:]))
		}
	}
}
