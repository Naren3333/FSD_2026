package main

import (
	_ "embed"
	"encoding/json"
	"slices"

	"learning/platform"
)

//go:embed operator-policy.json
var operatorPolicyJSON []byte

type operatorRoles struct {
	Required string   `json:"required_role"`
	Excluded []string `json:"excluded_roles"`
}

var operatorPolicy = func() operatorRoles {
	var policy operatorRoles
	if err := json.Unmarshal(operatorPolicyJSON, &policy); err != nil || policy.Required == "" || len(policy.Excluded) == 0 {
		panic("invalid monitoring operator policy")
	}
	return policy
}()

func isOperator(actor platform.Claims) bool {
	return actor.Has(operatorPolicy.Required) && !slices.ContainsFunc(operatorPolicy.Excluded, actor.Has)
}
