//go:build saga2

// 用真实原版备份逐资源核对 plan 声明的期望哈希。安装器对每个资源只做一次
// 全新生成；这里刻意在同一份原版字节上连续生成全部语言——既覆盖每个语言的
// 期望值，也钉住「生成不得改写输入」这一约束：任何格式若别名原版缓冲区，
// 第二个语言起就会失败。没有本地备份时跳过（公开检出不含游戏数据）。
package main

import (
	"os"
	"path/filepath"
	"sort"
	"testing"
)

func TestRecipeExpectedHashesRealBackup(t *testing.T) {
	game := os.Getenv("FARLAND_SAGA2_GAME")
	if game == "" {
		t.Skip("需要 FARLAND_SAGA2_GAME 指向二代游戏目录（内含原版备份）")
	}
	binPath := filepath.Join(game, "FarlandSaga2Patcher-backup", "FarlandSaga2.bin")
	exePath := filepath.Join(game, "FarlandSaga2Patcher-backup", "FarlandSaga2.exe")
	if _, err := os.Stat(binPath); err != nil {
		t.Skipf("原版备份缺失，跳过: %v", err)
	}
	plan, err := loadRecipePlan(embeddedPlanData())
	if err != nil {
		t.Fatal(err)
	}
	ids := make([]string, 0, len(plan.Resources))
	for rid := range plan.Resources {
		ids = append(ids, rid)
	}
	sort.Strings(ids)
	for _, rid := range ids {
		resource := plan.Resources[rid]
		original, err := readOriginalResource(binPath, exePath, plan, resource)
		if err != nil {
			t.Fatalf("读取原版 %s 失败: %v", rid, err)
		}
		for _, language := range plan.Languages {
			generated, err := applyRecipeResource(rid, resource, plan.blob, language, original)
			if err != nil {
				t.Fatalf("生成 %s/%s 失败: %v", rid, language, err)
			}
			expected := resource.Expected[language]
			if digestBytes(generated) != expected {
				t.Errorf("%s/%s: 期望 %s，实际 %s", rid, language, expected, digestBytes(generated))
			}
		}
	}
}
