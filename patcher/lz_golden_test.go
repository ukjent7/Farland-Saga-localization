//go:build !saga2

// PackLZ 与 Python 参考实现（fls.containers.overlay_font.pack_lz）的字节级
// 对拍。向量文件由确定性种子数据生成（random.seed(7) 的固定语料，见
// test-data/fs1-lz-vectors.json），更新向量必须同步审阅原因。
package main

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"os"
	"testing"
)

type lzVectorPair struct {
	input  []byte
	packed []byte
}

func loadLZVectors(t *testing.T) []struct {
	name string
	pair lzVectorPair
} {
	t.Helper()
	data, err := os.ReadFile("../test-data/fs1-lz-vectors.json")
	if err != nil {
		t.Skipf("缺少对拍向量 test-data/fs1-lz-vectors.json: %v", err)
	}
	var raw map[string][2]string
	if err := json.Unmarshal(data, &raw); err != nil {
		t.Fatalf("对拍向量解析失败: %v", err)
	}
	out := make([]struct {
		name string
		pair lzVectorPair
	}, 0, len(raw))
	for name, pair := range raw {
		input, err := hex.DecodeString(pair[0])
		if err != nil {
			t.Fatalf("%s: 输入解析失败: %v", name, err)
		}
		packed, err := hex.DecodeString(pair[1])
		if err != nil {
			t.Fatalf("%s: 输出解析失败: %v", name, err)
		}
		out = append(out, struct {
			name string
			pair lzVectorPair
		}{name, lzVectorPair{input: input, packed: packed}})
	}
	return out
}

func TestPackLZGolden(t *testing.T) {
	for _, item := range loadLZVectors(t) {
		got, err := PackLZ(item.pair.input)
		if err != nil {
			t.Fatalf("%s: PackLZ 失败: %v", item.name, err)
		}
		if !bytes.Equal(got, item.pair.packed) {
			t.Fatalf("%s: PackLZ 输出与 Python 参考不一致（got %d bytes, want %d bytes）",
				item.name, len(got), len(item.pair.packed))
		}
		back, err := UnpackLZ(got)
		if err != nil || !bytes.Equal(back, item.pair.input) {
			t.Fatalf("%s: PackLZ/UnpackLZ 往返失败", item.name)
		}
	}
}
