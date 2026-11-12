//go:build !saga2

// 一代的内嵌补丁来源：编译期生成的安装计划，按语言展开成 payload 列表。
// 语言 ZIP 不再进入发布构建；差异数据由安装计划携带。
package main

func embeddedPlanPayloads() ([]*payload, []string) {
	data := embeddedPlanData()
	if len(data) == 0 {
		return nil, nil
	}
	plan, err := loadFS1Plan(data)
	if err != nil {
		return nil, []string{"内嵌安装计划: " + err.Error()}
	}
	payloads := make([]*payload, 0, len(plan.languages))
	for _, language := range plan.languages {
		loaded, err := newFS1PlanPayload(plan, language)
		if err != nil {
			return nil, []string{"内嵌安装计划: " + err.Error()}
		}
		payloads = append(payloads, loaded)
	}
	return payloads, nil
}

func embeddedZipPayloads() ([]*payload, []string) { return nil, nil }
