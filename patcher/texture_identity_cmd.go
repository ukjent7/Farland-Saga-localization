//go:build !saga2

// Temporary helper: compute the rebuilt Texture.tex identity per language
// from the new ink-based declaration, for backfilling identities.json.
// Lives in package main so it can call applyTextureOp directly.
package main

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

type identityLayer struct {
	Mask        string `json:"mask"`
	Ink         string `json:"ink"`
	PatchedSHA  string `json:"patched_sha256"`
	PatchedSize int64  `json:"patched_size"`
}

type identityDoc struct {
	Schema    int                      `json:"schema"`
	File      string                   `json:"file"`
	Labels    json.RawMessage          `json:"labels"`
	Panels    json.RawMessage          `json:"panels"`
	PartCount int                      `json:"part_count"`
	ImageSize [2]int                   `json:"image_size"`
	Languages map[string]identityLayer `json:"languages"`
}

func runTextureIdentity(args []string) error {
	backup, declPath := args[0], args[1]
	raw, err := os.ReadFile(declPath)
	if err != nil {
		return err
	}
	var doc identityDoc
	if err := json.Unmarshal(raw, &doc); err != nil {
		return err
	}
	if doc.Schema != 2 || doc.File != "Texture.tex" {
		return fmt.Errorf("结构不符")
	}
	original, err := os.ReadFile(filepath.Join(backup, "Texture.tex"))
	if err != nil {
		return err
	}
	exePath := filepath.Join(backup, "FarlandSaga.exe")
	sourcePath := filepath.Join(backup, "Texture.tex")
	out := map[string]string{}
	for _, language := range []string{"zh-CN", "ko-KR", "en-US"} {
		section := doc.Languages[language]
		mask, err := base64.StdEncoding.DecodeString(section.Mask)
		if err != nil {
			return err
		}
		ink, err := base64.StdEncoding.DecodeString(section.Ink)
		if err != nil {
			return err
		}
		// 声明长度取决于自身数值位数，与编译器一样迭代到自洽；注意循环
		// 结束后必须再编码一次，保证 header 里的偏移是最终值。
		declaration := map[string]any{
			"image_size":  doc.ImageSize,
			"part_count":  doc.PartCount,
			"panels":      doc.Panels,
			"labels":      doc.Labels,
			"mask_offset": 0, "mask_length": len(mask),
			"ink_offset": 0, "ink_length": len(ink),
		}
		var header []byte
		for attempt := 0; attempt < 4; attempt++ {
			header, err = json.Marshal(declaration)
			if err != nil {
				return err
			}
			declaration["mask_offset"] = len(header)
			declaration["ink_offset"] = len(header) + len(mask)
			next, err := json.Marshal(declaration)
			if err != nil {
				return err
			}
			if len(next) == len(header) {
				break
			}
		}
		header, err = json.Marshal(declaration)
		if err != nil {
			return err
		}
		segment := append(append(append([]byte{}, header...), mask...), ink...)
		op := fs1PlanOp{Type: "texture", Name: "localized_ink",
			DataOffset: 0, DataLength: int64(len(segment)), Size: int64(len(header))}
		rebuilt, err := applyTextureOp(original, &op, &fs1PlanEntry{}, segment,
			sourcePath, exePath)
		if err != nil {
			return fmt.Errorf("%s: %w", language, err)
		}
		digest := sha256.Sum256(rebuilt)
		out[language] = hex.EncodeToString(digest[:]) + " " + fmt.Sprint(len(rebuilt))
	}
	encoded, _ := json.MarshalIndent(out, "", "  ")
	fmt.Println(string(encoded))
	return nil
}

func textureIdentityAvailable() bool { return true }
