// Cedar policy for the conformity-assistant AgentCore Gateway.
//
// HOW IT WORKS
// The Gateway's CUSTOM_JWT authorizer maps M2M JWT claims to Cedar principal
// tags. The claims used here are custom and are injected by the Cognito V3
// Pre-Token Lambda (infra-cdk/lambdas/pretoken-v3/index.py):
//
//   JWT claim "role"    -> principal.getTag("role")     ("technician" | "expert")
//   JWT claim "user_id" -> principal.getTag("user_id")  (Cognito sub)
//
// Cedar is deny-by-default: a tool with no matching `permit` is denied AND hidden
// from the agent at tool-discovery time (`tools/list`), so a technician cannot
// even see `record_validation`.
//
// ACTION NAME FORMAT: "<gateway target name>___<tool name>" (triple underscore).
//   targets: ncr-tool-target, case-tool-target, mail-tool-target
//   see the matching tool_spec.json for tool names.
//
// {{GATEWAY_ARN}} is replaced by CDK at deploy time with the real Gateway ARN.
//
// TO CHANGE: edit this file, then run `cdk deploy`.

// ---------------------------------------------------------------------------
// Read-only + reporting tools: every authenticated user (technician or expert).
// ---------------------------------------------------------------------------

permit(
  principal is AgentCore::OAuthUser,
  action in [
    AgentCore::Action::"ncr-tool-target___search_ncr_history",
    AgentCore::Action::"case-tool-target___create_case",
    AgentCore::Action::"case-tool-target___get_case",
    AgentCore::Action::"case-tool-target___list_cases",
    AgentCore::Action::"mail-tool-target___send_contractor_email",
    AgentCore::Action::"mail-tool-target___request_expert_validation"
  ],
  resource == AgentCore::Gateway::"{{GATEWAY_ARN}}"
)
when {
  principal.hasTag("role") &&
  (principal.getTag("role") == "technician" ||
   principal.getTag("role") == "expert")
};

// ---------------------------------------------------------------------------
// Expert-only: recording a validation decision.
// Technicians are denied by omission (deny-by-default), which also hides the
// tool from their agent session entirely.
// ---------------------------------------------------------------------------

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"case-tool-target___record_validation",
  resource == AgentCore::Gateway::"{{GATEWAY_ARN}}"
)
when {
  principal.hasTag("role") &&
  principal.getTag("role") == "expert"
};
