//go:build !saga2

// 一代补丁器没有 recipe 生成器；保留同名入口，让 main.go 与构建标签无关。
package main

import "errors"

func wrapRecipeSource(loaded *payload, planData []byte) (patchSource, error) {
	if len(planData) > 0 {
		return nil, errors.New("一代补丁器不支持 recipe plan")
	}
	return loaded, nil
}

func embeddedPlanData() []byte { return nil }
