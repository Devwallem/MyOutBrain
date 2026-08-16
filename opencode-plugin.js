const path = require('node:path');
const { randomUUID } = require('node:crypto');
const { MyOutBrainHarness, parseMcpCommand } = require('./src/myoutbrain-harness');

let toolBuilder = null;
let schemaRoot = null;

async function loadToolBuilder() {
  if (toolBuilder !== null) return { toolBuilder, schemaRoot };
  try {
    const pluginModule = await import('@opencode-ai/plugin');
    toolBuilder = pluginModule.tool;
    schemaRoot = pluginModule.tool?.schema;
  } catch (_error) {
    toolBuilder = (definition) => ({
      description: definition.description,
      args: definition.args || {},
      execute: definition.execute,
    });
    schemaRoot = null;
  }
  return { toolBuilder, schemaRoot };
}

const harnessByDir = new Map();

function memoryRootFromEnv(context) {
  const base = context?.directory ?? process.cwd();
  const fallback = path.resolve(base, '.myoutbrain');
  const configured = process.env.MYOUTBRAIN_MEMORY_ROOT
    || process.env.MYOUTBRAIN_ROOT
    || fallback;
  return path.resolve(configured);
}

function collectConfigOptions(root) {
  const options = {
    memoryRoot: root,
    pythonExecutable: process.env.MYOUTBRAIN_PYTHON_EXECUTABLE || 'python',
    startupTimeoutMs: Number(process.env.MYOUTBRAIN_STARTUP_TIMEOUT_MS) || 10_000,
    mcpCommand: parseMcpCommand(process.env.MYOUTBRAIN_MCP_COMMAND),
    environment: {
      ASTERFLOW_MYOUTBRAIN_MCP_COMMAND: process.env.ASTERFLOW_MYOUTBRAIN_MCP_COMMAND,
      MYOUTBRAIN_MEMORY_ROOT: process.env.MYOUTBRAIN_MEMORY_ROOT,
      MYOUTBRAIN_ROOT: process.env.MYOUTBRAIN_ROOT,
    },
  };
  if (process.env.MYOUTBRAIN_PACKAGE_ROOT) options.packageRoot = process.env.MYOUTBRAIN_PACKAGE_ROOT;
  return options;
}

function toOutput(value) {
  if (typeof value === 'string') return value;
  return JSON.stringify(value, null, 2);
}

function ensureObject(value, field) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${field} 必须是对象。`);
  }
  return value;
}

async function getHarness(context) {
  const root = memoryRootFromEnv(context);
  const existing = harnessByDir.get(root);
  if (existing) return existing;
  const harness = new MyOutBrainHarness(collectConfigOptions(root));
  harnessByDir.set(root, harness);
  await harness.start();
  return harness;
}

function explicitDecisionGuard(input) {
  if (input.explicitUserDecision !== true) {
    throw new Error('该操作需要 explicitUserDecision = true。');
  }
}

function createJsonSchemaMap() {
  const makeObject = (shape) => shape;
  return {
    memory_gateway: makeObject({
      request: {
        protocol: {
          major: 3,
          minor: 0,
        },
        client: {
          name: 'myoutbrain-openCode',
          capabilities: ['memory-graph.v3', 'knowledge-collector.v1'],
        },
      },
    }),
    memory_decide_card: {
      cardId: '',
      decision: '',
      explicitUserDecision: false,
    },
    memory_split_card: {
      cardId: '',
      cards: [],
      explicitUserDecision: false,
    },
    memory_decide_review: {
      proposalId: '',
      decision: '',
      explicitUserDecision: false,
    },
  };
}

function convertSchema(schema, zod) {
  if (!zod || !schema || typeof schema !== 'object') return schema;
  const toZod = (value) => {
    if (!value || typeof value !== 'object') return zod.unknown();
    if (value.type === 'string') return zod.string();
    if (value.type === 'number') return zod.number();
    if (value.type === 'boolean') return zod.boolean();
    if (value.type === 'array') return zod.array(toZod(value.items));
    if (value.type === 'object') {
      const props = value.properties || {};
      return zod.object(Object.fromEntries(Object.entries(props).map(([key, child]) => {
        const childSchema = toZod(child);
        return [key, childSchema];
      })));
    }
    if (Array.isArray(value.enum)) return zod.enum(value.enum);
    return zod.unknown();
  };
  return toZod(schema);
}

const MyOutBrainPlugin = async (context) => {
  const { toolBuilder: tool, schemaRoot: z } = await loadToolBuilder();
  const zod = z || null;
  const schemaMap = createJsonSchemaMap();

  return {
    tool: {
      myoutbrain_gateway: tool({
        description:
          'Invoke full MyOutBrain Domain Protocol 3.0 requests through the memory authority gateway.',
        args: zod?.object({
          request: zod?.object({
            protocol: zod?.object({
              major: zod?.number(),
              minor: zod?.number(),
            }),
            client: zod?.object({
              name: zod?.string(),
              capabilities: zod?.array(zod.string()),
            }),
            operation: zod?.string(),
            parameters: zod?.unknown(),
            idempotency_key: zod?.string().optional?.(),
          }),
        }) ?? {
          request: schemaMap.memory_gateway,
        },
        async execute(args) {
          const request = ensureObject(args, 'args').request;
          const harness = await getHarness(context);
          return toOutput(await harness.callGateway(request));
        },
      }),
      memory_list_collector_cards: tool({
        description: 'List pending and deferred temporary knowledge cards.',
        args: zod?.object({}) ?? {},
        async execute() {
          const harness = await getHarness(context);
          const cards = await harness.listCollectorCards();
          return toOutput({ cards });
        },
      }),
      memory_list_review_proposals: tool({
        description: 'List review proposals from memory inspection.',
        args: zod?.object({}) ?? {},
        async execute() {
          const harness = await getHarness(context);
          const proposals = await harness.listReviewProposals();
          return toOutput({ proposals });
        },
      }),
      memory_split_collector_card: tool({
        description: 'Split one pending/deferred collector card into multiple reviewable cards.',
        args: zod?.object({
          cardId: zod.string(),
          cards: zod.array(zod.object({
            problem: zod.string(),
            claim: zod.string(),
            evidence: zod.array(zod.string()).default([]),
            uncertainty: zod.array(zod.string()).default([]),
          })),
          explicitUserDecision: zod.boolean(),
        }) ?? schemaMap.memory_split_card,
        async execute(input) {
          explicitDecisionGuard(input);
          const cards = (input.cards ?? []).map((card) => ({
            problem: String(card.problem),
            claim: String(card.claim),
            evidence: Array.isArray(card.evidence) ? card.evidence.map(String) : [],
            uncertainty: Array.isArray(card.uncertainty) ? card.uncertainty.map(String) : [],
          }));
          const harness = await getHarness(context);
          return toOutput(await harness.splitCollectorCard(String(input.cardId), cards, 'user', randomUUID()));
        },
      }),
      memory_decide_collector_card: tool({
        description: 'Apply explicit decision for collector card lifecycle.',
        args: zod?.object({
          cardId: zod.string(),
          decision: zod.string(),
          explicitUserDecision: zod.boolean(),
        }) ?? schemaMap.memory_decide_card,
        async execute(input) {
          explicitDecisionGuard(input);
          const decision = String(input.decision);
          if (!['accept', 'reject', 'defer'].includes(decision)) {
            throw new Error('decision 需为 accept/reject/defer。');
          }
          const harness = await getHarness(context);
          return toOutput(await harness.decideCollectorCard(
            String(input.cardId),
            decision,
            'user',
            randomUUID(),
          ));
        },
      }),
      memory_decide_review_proposal: tool({
        description: 'Resolve one review proposal with explicit decision.',
        args: zod?.object({
          proposalId: zod.string(),
          decision: zod.string(),
          explicitUserDecision: zod.boolean(),
        }) ?? schemaMap.memory_decide_review,
        async execute(input) {
          explicitDecisionGuard(input);
          const decision = String(input.decision);
          if (!['accept', 'reject', 'defer'].includes(decision)) {
            throw new Error('decision 需为 accept/reject/defer。');
          }
          const harness = await getHarness(context);
          return toOutput(await harness.decideReviewProposal(
            String(input.proposalId),
            decision,
            'user',
            randomUUID(),
          ));
        },
      }),
    },
    config: (cfg) => {
      const mutable = cfg ?? {};
      mutable.tools = mutable.tools ?? {};
      return mutable;
    },
    close: async () => {
      for (const harness of harnessByDir.values()) {
        await harness.close();
      }
      harnessByDir.clear();
    },
  };
};

module.exports = MyOutBrainPlugin;
module.exports.default = MyOutBrainPlugin;
