package main

import (
	"bytes"
	"testing"
)

func TestLZRoundtrip(t *testing.T) {
	sample := bytes.Repeat([]byte("FarLandSaga Localization Steam Overlay Font Compression Test! 1234567890\x00\xff"), 10)
	packed := PackLZ(sample)
	unpacked, err := UnpackLZ(packed)
	if err != nil {
		t.Fatalf("UnpackLZ 失败: %v", err)
	}
	if !bytes.Equal(sample, unpacked) {
		t.Fatalf("LZ 压缩解压未能无损还原数据")
	}
}

func TestExpandFont(t *testing.T) {
	// 32 字节代表一个 16x16 的字符，全部为 0xFF
	mono := bytes.Repeat([]byte{0xFF}, 32)
	rgba := ExpandFont(mono)
	if len(rgba) != 1024 {
		t.Fatalf("ExpandFont 长度不符: 期望 1024, 实际 %d", len(rgba))
	}
	for i := 0; i < 256; i++ {
		// 每个像素 4 字节，RGBA 中的 A (偏移 3) 应该为 255
		if rgba[i*4+3] != 255 {
			t.Fatalf("像素 %d Alpha 通道不为 255", i)
		}
	}
}
