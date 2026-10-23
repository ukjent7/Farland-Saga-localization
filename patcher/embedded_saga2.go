//go:build saga2

// 二代发布版只内嵌 recipe plan，不再携带任何补丁 ZIP。plan 由公开仓库的
// recipe/ 与 OFL 字体在构建时生成，安装器再从用户自己的原版文件派生出补丁
// 资源。
package main

func embeddedPlanPayloads() ([]*payload, []string) {
	data := embeddedPlanData()
	if len(data) == 0 {
		return nil, nil
	}
	plan, err := loadRecipePlan(data)
	if err != nil {
		return nil, []string{"内嵌 recipe plan: " + err.Error()}
	}
	payloads := make([]*payload, 0, len(plan.Languages))
	for _, language := range plan.Languages {
		loaded, err := newRecipePayload(plan, language)
		if err != nil {
			return nil, []string{"内嵌 recipe plan: " + err.Error()}
		}
		payloads = append(payloads, loaded)
	}
	return payloads, nil
}

func embeddedZipPayloads() ([]*payload, []string) { return nil, nil }
