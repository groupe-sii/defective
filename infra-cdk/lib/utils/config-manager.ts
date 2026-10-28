import * as fs from "fs"
import * as path from "path"
import * as yaml from "yaml"

const MAX_STACK_NAME_BASE_LENGTH = 35

export type DeploymentType = "docker" | "zip"

/**
 * Network mode for the AgentCore Runtime.
 * - PUBLIC: Runtime is accessible over the public internet (default).
 * - VPC: Runtime is deployed into a user-provided VPC for private network isolation.
 */
export type NetworkMode = "PUBLIC" | "VPC"

/**
 * VPC configuration for deploying the AgentCore Runtime into an existing VPC.
 * Required when network_mode is "VPC".
 */
export interface VpcConfig {
  /** The ID of the existing VPC to deploy into (e.g. "vpc-0abc1234def56789a"). */
  vpc_id: string
  /** List of subnet IDs within the VPC where the runtime will be placed. */
  subnet_ids: string[]
  /** Optional list of security group IDs. If omitted, a default security group is created. */
  security_group_ids?: string[]
}

export interface AppConfig {
  stack_name_base: string
  admin_user_email?: string | null
  backend: {
    pattern: string
    deployment_type: DeploymentType
    /** Name for the agent runtime. Valid characters: a-z, A-Z, 0-9, _. Defaults to "ConformityAgent". */
    agent_name: string
    /** Network mode for the AgentCore Runtime. Defaults to "PUBLIC". */
    network_mode: NetworkMode
    /** VPC configuration. Required when network_mode is "VPC". */
    vpc?: VpcConfig
    /**
     * Enable long-term memory (SemanticMemoryStrategy) for the agent.
     * When true, the agent extracts and retrieves facts across sessions.
     * This incurs additional costs: $0.75/1,000 records stored + $0.50/1,000 retrievals.
     * Defaults to false.
     */
    use_long_term_memory: boolean
    /**
     * Number of facts to retrieve per turn when long-term memory is enabled.
     * Maps to the top_k parameter of RetrievalConfig. Defaults to 10.
     */
    ltm_top_k: number
    /**
     * Minimum similarity threshold for long-term memory retrieval.
     * Maps to the relevance_score parameter of RetrievalConfig. Defaults to 0.3.
     */
    ltm_relevance_score: number
    /**
     * Discover and auto-connect MCP servers from an AWS Agent Registry.
     * Lightweight: no DynamoDB, no UI, no per-user preferences. Defaults to disabled.
     */
    mcp_registry: McpRegistryConfig
  }
  /**
   * Domain configuration for the conformity assistant (this fork's use case).
   * All keys have safe defaults so a plain upstream-template deployment still works.
   */
  conformity?: ConformityConfig
}

/**
 * SMS delivery settings (AWS End User Messaging / pinpoint-sms-voice-v2).
 */
export interface SmsConfig {
  /**
   * Origination identity for outbound SMS: a phone-number id/ARN, a pool id, or
   * an alphanumeric sender ID. France offers no long codes through the API but
   * alphanumeric sender IDs need no registration (letters/digits only).
   */
  origination_identity: string
  /** TRANSACTIONAL (default) or PROMOTIONAL. */
  message_type: string
  /**
   * Per-destination-country origination overrides (ISO 3166 alpha-2 -> phone
   * number id/ARN/sender ID). EUM has no origination that reaches every country:
   * the simulator is US-only, France needs the CONFORMITE sender ID.
   */
  origination_by_country: { [country: string]: string }
  /** Demo identities (digits only). simulator destination or a real handset. */
  demo_identities: SmsDemoIdentities
}

/**
 * The phone identities used by the demo scripts. Either may be an EUM simulator
 * destination or a verified real handset — the scripts do not care, and the
 * dispatcher picks the origination by destination country.
 */
export interface SmsDemoIdentities {
  /** Reports the non-conformity. */
  technician: string
  /** Approves or rejects the drafted procedure. */
  expert: string
}

/**
 * Conformity-assistant domain configuration. Ported from the POC's
 * template.yaml parameters (KB id, model, locale, expert contacts, mock flags).
 */
export interface ConformityConfig {
  /** Region for the whole conformity stack. Defaults to the CDK deploy region. */
  region: string
  /** Bedrock model / inference profile id used by the agent. */
  model_id: string
  /** Bedrock Knowledge Base id holding past NCRs. */
  knowledge_base_id: string
  /** Prompt locale. Supported: en_US, pt_BR, fr_FR. */
  locale: string
  /** Stream the model response (ConverseStream) or not (Converse). */
  streaming: boolean
  /** Channels answered in demo mode (CSV). Listed channel = log, absent = send. */
  mock_channels: string
  /** When true, send_contractor_email logs instead of sending via SES. */
  mock_email: boolean
  /** From address for the contractor notification e-mail. */
  contractor_email_from: string
  /** SMS origination identity (phone-number id/ARN, pool id, or sender ID). */
  sms: SmsConfig
  /** Expert phone numbers (digits only) seeded into the contacts mapping table. */
  expert_contacts: string[]
  /**
   * Enforcement mode of the AgentCore policy engine attached to the Gateway.
   * `ENFORCE` really denies; `LOG_ONLY` only traces the decision (useful when
   * validating a policy change). Defaults to ENFORCE.
   */
  policy_engine_mode: "ENFORCE" | "LOG_ONLY"
}

/**
 * Runtime MCP-server discovery from an AWS Agent Registry.
 *
 * When enabled, the agent lists the registry's Approved `recordType=MCP`
 * records and auto-connects to each public streamable-HTTP server as a live
 * MCP client. Discovery happens at agent runtime, so no servers are declared
 * at deploy time and no gateway targets are created.
 */
export interface McpRegistryConfig {
  /** Master switch. When false (default) the feature is completely inert. */
  enabled: boolean
  /** ARN or id of the AWS Agent Registry to discover records from. Required when enabled. */
  registry_id: string
}

export class ConfigManager {
  private config: AppConfig

  constructor(configFile: string) {
    this.config = this._loadConfig(configFile)
  }

  private _loadConfig(configFile: string): AppConfig {
    let configPath: string

    // Uses the specified configFile if the file exists
    // otherwise fallsback to existing behavior where the configFile should be
    // named config.yaml and be in the infra-cdk directory. Throws an error if the
    // configFile does not exist and is not the default "config.yaml"
    if (fs.existsSync(configFile)) {
      configPath = configFile
    } else {
      if (path.basename(configFile) !== "config.yaml") {
        throw new Error(`Configuration file '${configFile}' not found.`)
      }
      const defaultConfigPath = path.join(__dirname, "..", "..", configFile) // nosemgrep: javascript.lang.security.audit.path-traversal.path-join-resolve-traversal.path-join-resolve-traversal
      configPath = defaultConfigPath
    }
    if (!fs.existsSync(configPath)) {
      throw new Error(
        `Configuration file ${configPath} does not exist. Please create config.yaml file.`
      )
    }

    try {
      const fileContent = fs.readFileSync(configPath, "utf8")
      const parsedConfig = yaml.parse(fileContent) as AppConfig

      const deploymentType = parsedConfig.backend?.deployment_type || "docker"
      if (deploymentType !== "docker" && deploymentType !== "zip") {
        throw new Error(
          `Invalid deployment_type '${deploymentType}' in ${configPath}. Must be 'docker' or 'zip'.`
        )
      }

      const stackNameBase = parsedConfig.stack_name_base
      if (!stackNameBase) {
        throw new Error(`stack_name_base is required in ${configPath}`)
      }
      if (stackNameBase.length > MAX_STACK_NAME_BASE_LENGTH) {
        throw new Error(
          `stack_name_base '${stackNameBase}' is too long (${stackNameBase.length} chars). ` +
            `Maximum length is ${MAX_STACK_NAME_BASE_LENGTH} characters due to AWS AgentCore runtime naming constraints.`
        )
      }

      // Validate network_mode if provided
      const networkMode = parsedConfig.backend?.network_mode || "PUBLIC"
      if (networkMode !== "PUBLIC" && networkMode !== "VPC") {
        throw new Error(
          `Invalid network_mode '${networkMode}' in ${configPath}. Must be 'PUBLIC' or 'VPC'.`
        )
      }

      // Validate VPC configuration when network_mode is VPC
      const vpcConfig = parsedConfig.backend?.vpc
      if (networkMode === "VPC") {
        if (!vpcConfig) {
          throw new Error(
            `backend.vpc configuration is required in ${configPath} when network_mode is 'VPC'.`
          )
        }
        if (!vpcConfig.vpc_id) {
          throw new Error(
            `backend.vpc.vpc_id is required in ${configPath} when network_mode is 'VPC'.`
          )
        }
        if (!vpcConfig.subnet_ids || vpcConfig.subnet_ids.length === 0) {
          throw new Error(
            `backend.vpc.subnet_ids must contain at least one subnet ID in ${configPath} when network_mode is 'VPC'.`
          )
        }
      }

      // Validate MCP registry discovery configuration.
      // Fail loud: enabling discovery without a registry id is a deploy-time mistake.
      const mcpRegistryEnabled = parsedConfig.backend?.mcp_registry?.enabled === true
      const mcpRegistryId = (parsedConfig.backend?.mcp_registry?.registry_id ?? "").trim()
      if (mcpRegistryEnabled && !mcpRegistryId) {
        throw new Error(
          `backend.mcp_registry.registry_id is required in ${configPath} when backend.mcp_registry.enabled is true.`
        )
      }

      return {
        stack_name_base: stackNameBase,
        admin_user_email: parsedConfig.admin_user_email || null,
        backend: {
          pattern: parsedConfig.backend?.pattern || "strands-single-agent",
          deployment_type: deploymentType,
          agent_name: parsedConfig.backend?.agent_name || "ConformityAgent",
          network_mode: networkMode,
          vpc: vpcConfig,
          use_long_term_memory: parsedConfig.backend?.use_long_term_memory === true,
          ltm_top_k: parsedConfig.backend?.ltm_top_k ?? 10,
          ltm_relevance_score: parsedConfig.backend?.ltm_relevance_score ?? 0.3,
          mcp_registry: {
            enabled: mcpRegistryEnabled,
            registry_id: mcpRegistryId,
          },
        },
        conformity: {
          region: parsedConfig.conformity?.region || process.env.CDK_DEFAULT_REGION || "eu-west-1",
          model_id: parsedConfig.conformity?.model_id || "eu.amazon.nova-pro-v1:0",
          knowledge_base_id: parsedConfig.conformity?.knowledge_base_id || "",
          locale: parsedConfig.conformity?.locale || "en_US",
          streaming: parsedConfig.conformity?.streaming !== false,
          mock_channels: parsedConfig.conformity?.mock_channels || "whatsapp,sms,email,rcs",
          mock_email: parsedConfig.conformity?.mock_email !== false,
          contractor_email_from:
            parsedConfig.conformity?.contractor_email_from || "noreply@example.com",
          sms: {
            origination_identity: parsedConfig.conformity?.sms?.origination_identity || "",
            message_type: parsedConfig.conformity?.sms?.message_type || "TRANSACTIONAL",
            origination_by_country: parsedConfig.conformity?.sms?.origination_by_country || {},
            demo_identities: {
              technician: parsedConfig.conformity?.sms?.demo_identities?.technician
                || "14254147755",
              expert: parsedConfig.conformity?.sms?.demo_identities?.expert
                || "14254147167",
            },
          },
          expert_contacts: parsedConfig.conformity?.expert_contacts || [],
          policy_engine_mode:
            parsedConfig.conformity?.policy_engine_mode === "LOG_ONLY"
              ? "LOG_ONLY"
              : "ENFORCE",
        },
      }
    } catch (error) {
      throw new Error(`Failed to parse configuration file ${configPath}: ${error}`)
    }
  }

  public getProps(): AppConfig {
    return this.config
  }

  public get(key: string, defaultValue?: any): any {
    const keys = key.split(".")
    let value: any = this.config

    for (const k of keys) {
      if (typeof value === "object" && value !== null && k in value) {
        // nosemgrep: javascript.lang.security.audit.prototype-pollution.prototype-pollution-loop.prototype-pollution-loop — iterates over a trusted local YAML config object, not user-controlled input
        value = value[k]
      } else {
        return defaultValue
      }
    }

    return value
  }
}
