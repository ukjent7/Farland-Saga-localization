//go:build !saga2

// 一代 recipe 入口：编译好的安装计划与语言选择组合成一个 patchSource。
package main

import (
	"embed"
	"errors"
)

// plan/saga1/.gitkeep 保持 embed 指令在干净的公开检出里有效；发布构建先用
// tools/compile_recipe_fs1.py 从 recipe/fs1 的语言载荷编译出 plan.bin，
// 与二代由 compile_recipe.py 编译 plan 的流程同构。
//
//go:embed all:plan/saga1
var embeddedFS1PlanFS embed.FS

func embeddedPlanData() []byte {
	data, err := embeddedFS1PlanFS.ReadFile("plan/saga1/plan.bin")
	if err != nil {
		return nil
	}
	return data
}

func wrapRecipeSource(loaded *payload, planData []byte, language string) (patchSource, error) {
	if len(planData) == 0 {
		if loaded == nil {
			return nil, errors.New("没有可用的安装计划或补丁载荷")
		}
		return loaded, nil
	}
	plan, err := loadFS1Plan(planData)
	if err != nil {
		return nil, err
	}
	if language == "" {
		if loaded != nil {
			language = loaded.info.Language
		} else if len(plan.languages) == 1 {
			language = plan.languages[0]
		} else {
			return nil, errors.New("安装计划包含多种语言，请用 --lang 指定")
		}
	}
	return newFS1PlanPayload(plan, language)
}
