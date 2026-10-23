//go:build saga2

// 二代 recipe 入口：编译好的 plan 与补丁载荷组合成一个 patchSource。
package main

import (
	"embed"
	"errors"
	"fmt"
)

// plan/.gitkeep keeps the embed directive valid in a clean public checkout;
// private builds replace it with plan.bin compiled from recipe/.
//
//go:embed all:plan
var embeddedPlanFS embed.FS

func embeddedPlanData() []byte {
	data, err := embeddedPlanFS.ReadFile("plan/plan.bin")
	if err != nil {
		return nil
	}
	return data
}

func wrapRecipeSource(loaded *payload, planData []byte) (patchSource, error) {
	if len(planData) == 0 {
		return loaded, nil
	}
	plan, err := loadRecipePlan(planData)
	if err != nil {
		return nil, err
	}
	return newRecipeSource(loaded, plan, loaded.info.Language)
}

func applyRecipeResource(rid string, resource *recipeResource, blob []byte, language string, original []byte) ([]byte, error) {
	switch resource.Format {
	case "tile_atlas_8bpp":
		atlas, err := decodeTileAtlas(original)
		if err != nil {
			return nil, fmt.Errorf("%s: %w", rid, err)
		}
		operations, err := planOperations(resource, language)
		if err != nil {
			return nil, err
		}
		if _, err := applyTileAtlasOperations(atlas, operations, blob); err != nil {
			return nil, fmt.Errorf("%s: %w", rid, err)
		}
		if _, err := repackTileAtlas(atlas); err != nil {
			return nil, fmt.Errorf("%s: %w", rid, err)
		}
		return atlas.raw, nil
	default:
		return nil, fmt.Errorf("%s: 不支持的资源格式 %q", rid, resource.Format)
	}
}

func planOperations(resource *recipeResource, language string) ([]recipeOperation, error) {
	operations, ok := resource.Operations[language]
	if !ok {
		return nil, errors.New("recipe 缺少该语言的操作")
	}
	return operations, nil
}
